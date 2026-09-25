"""Phase 1 of the first-pass classification contract: shadow, and nothing else.

Specification: ``docs/plans/2026-09-24-first-pass-classification-contract-design.md``
§8 阶段 1, §10 A2/A3/A4, B1/B2/B3, E1/E2/E5.

Two claims are under test here and they are different claims:

1. the new field survives the write/read round trip, because §10 B1/B2 warns
   that writing without reading loses it silently on the replay path; and
2. carrying the field changes **no** behaviour -- same triggers, same
   instructions, same execution decisions with and without it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from telegram_kol_research.authoritative_instructions import (
    normalize_authoritative_instructions,
)
from telegram_kol_research.authoritative_recognition import (
    _load_current_mimo_evidence_result,
    compare_assessments,
    requires_context_resolution,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_classification import parse_message_classes
from telegram_kol_research.message_evidence import (
    normalize_mimo_evidence,
    save_message_evidence_version,
)
from telegram_kol_research.models import RawMessage
from telegram_kol_research.prompt_composition import validate_prompt_content
from telegram_kol_research.prompt_defaults import (
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    SHARED_TRADING_PROMPT,
)
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_pending_authoritative_decision,
)
from telegram_kol_research.recognition_experiments import (
    _validate_authoritative_payload,
)


EXIT_AND_REVERSE_CLASSES = [
    {
        "class": "仓位管理",
        "target": {
            "resolution": "exact",
            "lifecycle_id": 1319,
            "symbol": "ETH",
            "side": "short",
        },
    },
    {"class": "新策略", "target": None},
]


def _base_payload():
    """A first-pass payload in the shape production writes today."""

    return {
        "instructions": [
            {
                "kind": "full_exit",
                "confidence": 0.9,
                "reason": "平掉 ETH 空单",
                "strategy": None,
                "target": {"lifecycle_id": 1319, "thread_id": None},
                "parameters": {},
            },
            {
                "kind": "entry",
                "confidence": 0.9,
                "reason": "反手做多",
                "strategy": {
                    "symbol": "ETH",
                    "side": "long",
                    "entry": "3120",
                    "stop_loss": "3040",
                    "take_profit": None,
                    "leverage": None,
                    "order_type": "limit",
                },
                "target": {"lifecycle_id": None, "thread_id": None},
                "parameters": {},
            },
        ],
        "recognition_result": "是策略",
        "reason": "平掉空单并反手做多",
        "strategy": {
            "symbol": "ETH",
            "side": "long",
            "entry": "3120",
            "stop_loss": "3040",
            "take_profit": None,
            "leverage": None,
            "order_type": "limit",
        },
        "lifecycle_event": {
            "event_type": "exit_position",
            "target_lifecycle_id": 1319,
            "symbol": "ETH",
            "side": "short",
            "management_action": None,
            "confidence": 0.9,
            "reason": "平掉 ETH 空单",
        },
        "evidence": {"text": {"observed_text": "平掉，反手做多"}, "images": [], "conflicts": []},
        "input_reading": {"observed_text": "平掉，反手做多", "image_quality": "none"},
        "confidence": 0.9,
    }


def _with_classes():
    payload = _base_payload()
    payload["message_classes"] = [dict(item) for item in EXIT_AND_REVERSE_CLASSES]
    return payload


def _message(session_factory, message_id=4400):
    with session_factory() as session:
        row = RawMessage(
            chat_id=-1001,
            message_id=message_id,
            posted_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
            text="平掉，反手做多",
            archived_target_group=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


# --------------------------------------------------------------------------
# §10 A2 / A3: the seed and the publication gate
# --------------------------------------------------------------------------


def test_the_seed_prompt_defines_the_classification_contract():
    prompt = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT

    assert '"message_classes"' in prompt
    assert '"class"' in prompt
    assert '"resolution": "exact | forthcoming | unknown"' in prompt
    for value in ("新策略", "策略管理", "仓位管理", "闲话", "图片不可读"):
        assert value in prompt
    # It is the message's primary conclusion, so it leads the output object.
    assert prompt.index('"message_classes"') < prompt.index('"recognition_result"')
    # §8 阶段 1: no existing field is removed.
    for legacy in ('"lifecycle_event"', '"entry_fragments"', '"input_reading"'):
        assert legacy in prompt


def test_the_seed_prompt_still_passes_its_own_validation():
    result = validate_prompt_content(
        SHARED_TRADING_PROMPT,
        DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
        validation_profile="trading_shared",
        required_variables=(),
    )

    assert result.success is True, result.errors


def test_a_shared_template_without_the_new_field_can_no_longer_be_published():
    """§10 A3: 缺字段的提示词版本从此发不出去。"""

    stripped = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT.replace("message_classes", "xx")

    result = validate_prompt_content(
        SHARED_TRADING_PROMPT,
        stripped,
        validation_profile="trading_shared",
        required_variables=(),
    )

    assert result.success is False
    assert any("message_classes" in error for error in result.errors)


def test_the_gate_no_longer_demands_a_field_the_live_prompt_has_never_had():
    """A v8-lineage template plus the new block must be publishable.

    The live version (``ai_prompt_versions.id = 8``, published 2026-08-05) has
    never carried ``entry_fragments``; the marker demanding it was added to this
    gate afterwards, and because the gate runs on save/publish and never on
    render, nothing surfaced it. The effect was that no new version built on the
    running prompt could be published at all. This pins the shape that had to
    become publishable: everything the live version has, plus the
    classification block, and no ``entry_fragments`` anywhere.
    """

    i0 = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT.index("【消息分类 message_classes】")
    i1 = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT.index("【新开仓识别】")
    classification_block = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT[i0:i1]
    k0 = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT.index('  "message_classes": [')
    k1 = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT.index('  "instructions": [')
    json_block = DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT[k0:k1]
    v8_lineage = "\n".join(
        [
            "你是 Telegram 加密货币 KOL 消息的交易策略分析器。",
            classification_block.replace("entry_fragments", "entry_context"),
            "【新开仓识别】",
            "- lifecycle_event 的 event_type 可为 none、entry_confirm、cancel_entry、"
            "exit_position、position_update。",
            "- order_type 只能是 market、limit、market+limit；side 只能是 long 或 short。",
            "只输出一个 JSON 对象：",
            "{",
            json_block,
            '  "recognition_result": "是策略 | 非策略 | 识别失败",',
            '  "reason": "",',
            '  "strategy": {"symbol": null, "side": null, "entry": null,',
            '    "stop_loss": null, "take_profit": null, "leverage": null,',
            '    "order_type": null},',
            '  "lifecycle_event": {"event_type": "none", "target_lifecycle_id": null,',
            '    "management_action": null},',
            '  "input_reading": {"observed_text": "", "image_quality": "none"},',
            '  "confidence": 0.0',
            "}",
        ]
    )

    assert "entry_fragments" not in v8_lineage

    result = validate_prompt_content(
        SHARED_TRADING_PROMPT,
        v8_lineage,
        validation_profile="trading_shared",
        required_variables=(),
    )

    assert result.success is True, result.errors


def test_the_validation_gate_is_not_on_the_render_path_of_the_active_prompt():
    """The active production version must never be rejected at recognition time.

    ``compose_trading_prompt`` is what the first pass calls; it resolves the
    active version and does not validate it, which is why adding a marker cannot
    break a running v8.
    """

    import inspect

    from telegram_kol_research import prompt_composition

    source = inspect.getsource(prompt_composition.compose_trading_prompt)

    assert "validate_prompt_content" not in source


# --------------------------------------------------------------------------
# §10 A4: the recognition-side parse records, it does not raise
# --------------------------------------------------------------------------


def test_recognition_accepts_a_payload_without_the_new_field():
    """A prompt rollback to v8 must not break recognition."""

    _validate_authoritative_payload(_base_payload())


@pytest.mark.parametrize(
    "classes",
    [
        None,
        [],
        "仓位管理",
        [{"class": "识别失败"}],
        [{"class": "仓位管理", "target": None}],
        [{"class": "闲话", "target": None}, {"class": "新策略", "target": None}],
    ],
)
def test_recognition_accepts_a_payload_whose_new_field_violates_the_contract(classes):
    """Phase 1 records violations; promoting them to a failure is phase 3."""

    payload = _base_payload()
    payload["message_classes"] = classes

    _validate_authoritative_payload(payload)


def test_recognition_still_rejects_the_pre_existing_broken_payloads():
    """Adding the parse must not have loosened the checks that were there."""

    missing_strategy = _with_classes()
    missing_strategy.pop("strategy")
    bad_result = _with_classes()
    bad_result["recognition_result"] = "也许"

    with pytest.raises(ValueError):
        _validate_authoritative_payload(missing_strategy)
    with pytest.raises(ValueError):
        _validate_authoritative_payload(bad_result)


# --------------------------------------------------------------------------
# §10 B1 / B2: the write/read round trip
# --------------------------------------------------------------------------


def test_normalized_evidence_stores_the_classification_and_its_violations():
    _, _, _, _, normalized = normalize_mimo_evidence(
        _with_classes(), input_kind="text", error_message=None
    )

    assert normalized["message_classes"] == EXIT_AND_REVERSE_CLASSES
    assert "message_classes_violations" not in normalized

    broken = _with_classes()
    broken["message_classes"] = [{"class": "仓位管理", "target": None}]
    broken["strategy"] = None
    _, _, _, _, normalized_broken = normalize_mimo_evidence(
        broken, input_kind="text", error_message=None
    )

    assert normalized_broken["message_classes_violations"] == ["target_required"]


def test_normalized_evidence_omits_the_key_entirely_for_an_old_prompt_version():
    _, _, _, _, normalized = normalize_mimo_evidence(
        _base_payload(), input_kind="text", error_message=None
    )

    assert "message_classes" not in normalized
    assert "message_classes_violations" not in normalized


def test_the_classification_survives_the_evidence_round_trip(tmp_path):
    """§10 B1 writes it and §10 B2 reads it; a missing B2 loses it silently."""

    session_factory = create_session_factory(tmp_path / "research.db")
    message = _message(session_factory)
    payload = _with_classes()
    payload["message_classes"][0]["target"]["resolution"] = "exact"
    broken_payload = dict(payload)
    broken_payload["message_classes"] = [{"class": "仓位管理", "target": None}]

    for source in (payload, broken_payload):
        (
            extraction_status,
            confidence,
            text_evidence,
            image_evidence,
            normalized,
        ) = normalize_mimo_evidence(source, input_kind="text", error_message=None)
        save_message_evidence_version(
            session_factory,
            raw_message_id=message.id,
            input_fingerprint=f"sha256:{id(source)}",
            model="mimo-v2.5",
            prompt_versions={"trading.analysis.shared": 9},
            extraction_status=extraction_status,
            confidence=confidence,
            text_evidence=text_evidence,
            image_evidence=image_evidence,
            normalized_evidence=normalized,
        )
        loaded = _load_current_mimo_evidence_result(session_factory, message.id)
        assert loaded is not None
        reconstructed = loaded[0].payload
        expected = parse_message_classes(source)
        assert reconstructed["message_classes"] == expected.to_payload()
        assert (
            reconstructed.get("message_classes_violations", [])
            == list(expected.violations)
        )


def test_an_evidence_row_written_before_the_contract_reads_back_unchanged(tmp_path):
    session_factory = create_session_factory(tmp_path / "legacy.db")
    message = _message(session_factory, message_id=4401)
    (
        extraction_status,
        confidence,
        text_evidence,
        image_evidence,
        normalized,
    ) = normalize_mimo_evidence(_base_payload(), input_kind="text", error_message=None)
    save_message_evidence_version(
        session_factory,
        raw_message_id=message.id,
        input_fingerprint="sha256:legacy",
        model="mimo-v2.5",
        prompt_versions={"trading.analysis.shared": 8},
        extraction_status=extraction_status,
        confidence=confidence,
        text_evidence=text_evidence,
        image_evidence=image_evidence,
        normalized_evidence=normalized,
    )

    loaded = _load_current_mimo_evidence_result(session_factory, message.id)

    assert loaded is not None
    assert "message_classes" not in loaded[0].payload


def test_the_stored_form_is_idempotent_under_a_second_parse():
    """Re-normalizing what was stored must produce the same bytes.

    Without this, a replay would write a payload that differs from the live one
    purely through normalization, which is what §10 B3 is worried about.
    """

    parsed = parse_message_classes(_with_classes())
    stored = parsed.to_payload()
    reparsed = parse_message_classes({**_with_classes(), "message_classes": stored})

    assert reparsed.to_payload() == stored
    assert reparsed.violations == parsed.violations


# --------------------------------------------------------------------------
# §10 B3: the `changed` comparison in recognition_decisions
# --------------------------------------------------------------------------


def _record(payload, raw_message_id=4400):
    return RecognitionDecisionRecord(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="是策略",
        authoritative_payload=payload,
        auxiliary_model=None,
        auxiliary_status=None,
        auxiliary_payload=None,
        agreement_status="pending",
        differences=[],
        prompt_versions={"trading.analysis.shared": 9},
    )


def test_an_unchanged_payload_carrying_the_new_field_is_still_unchanged(tmp_path):
    """§10 B3: the extra field must not make an identical payload look changed.

    ``changed`` decides whether a completed comparison survives. Saving the same
    payload twice must leave the auxiliary review in place, exactly as it does
    without the field.
    """

    session_factory = create_session_factory(tmp_path / "decisions.db")
    message = _message(session_factory)
    payload = _with_classes()

    save_pending_authoritative_decision(session_factory, _record(payload, message.id))
    with session_factory() as session:
        from telegram_kol_research.models import RecognitionDecision

        row = session.query(RecognitionDecision).one()
        row.comparison_status = "completed"
        row.auxiliary_model = "deepseek"
        row.agreement_status = "agreed"
        session.commit()

    saved = save_pending_authoritative_decision(
        session_factory, _record(dict(payload), message.id)
    )

    # preserve_completed_review kept the auxiliary side alive: the payload was
    # byte-identical, so nothing was reset.
    assert saved.auxiliary_model == "deepseek"
    assert saved.agreement_status == "agreed"
    assert json.loads(saved.authoritative_payload_json)["message_classes"] == (
        EXIT_AND_REVERSE_CLASSES
    )


def test_adding_the_field_does_not_change_whether_two_payloads_compare_equal(
    tmp_path,
):
    """The field is inert for equality: it only differs when it really differs."""

    from telegram_kol_research.recognition_decisions import _json

    without_a = _base_payload()
    without_b = _base_payload()
    with_a = _with_classes()
    with_b = _with_classes()
    with_other = _with_classes()
    with_other["message_classes"] = [{"class": "闲话", "target": None}]

    assert _json(without_a) == _json(without_b)
    assert _json(with_a) == _json(with_b)
    assert _json(with_a) != _json(without_a)
    assert _json(with_a) != _json(with_other)


# --------------------------------------------------------------------------
# Behaviour is unchanged: same payload, with and without the field
# --------------------------------------------------------------------------


def _trigger(payload):
    return requires_context_resolution(
        first_pass_payload=payload,
        evidence={"conflicts": []},
        context_window={
            "current": {"text": "平掉，反手做多"},
            "reply_chain": [],
        },
        candidates=[{"thread_id": 12, "lifecycle_id": 1319}],
    )


@pytest.mark.parametrize(
    "classes",
    [
        EXIT_AND_REVERSE_CLASSES,
        [{"class": "闲话", "target": None}],
        [
            {
                "class": "仓位管理",
                "target": {"resolution": "unknown", "lifecycle_id": None},
            }
        ],
        [],
        None,
        "broken",
    ],
)
def test_the_new_field_changes_neither_triggers_nor_instructions(classes):
    """The zero-behaviour-change regression for phase 1.

    Whatever the classification says -- including a value that contradicts the
    old fields, and including a malformed one -- the context trigger reasons and
    the normalized instructions must be byte-identical to the same payload
    without the field. If §5's rewrite ever lands early, or a reader starts
    branching on the field, this goes red.
    """

    without = _base_payload()
    with_classes = _base_payload()
    with_classes["message_classes"] = classes

    assert _trigger(with_classes) == _trigger(without)
    assert normalize_authoritative_instructions(
        with_classes
    ) == normalize_authoritative_instructions(without)


def test_a_management_message_keeps_its_triggers_when_classified_explicitly():
    """The same claim on the shape §5 will eventually key on (resolution=unknown)."""

    without = {
        "recognition_result": "非策略",
        "strategy": None,
        "lifecycle_event": {"event_type": "position_update", "target_lifecycle_id": None},
    }
    with_classes = dict(without)
    with_classes["message_classes"] = [
        {"class": "仓位管理", "target": {"resolution": "unknown", "lifecycle_id": None}}
    ]
    resolved_instead = dict(without)
    resolved_instead["message_classes"] = [
        {"class": "仓位管理", "target": {"resolution": "exact", "lifecycle_id": 1319}}
    ]

    baseline = _trigger(without)

    # Both an "unknown" and an "exact" classification leave the current trigger
    # judgement exactly as it was: phase 1 does not rewrite §5.
    assert _trigger(with_classes) == baseline
    assert _trigger(resolved_instead) == baseline


# --------------------------------------------------------------------------
# §10 E5: compare_assessments feeds the prompt centre's A/B test
# --------------------------------------------------------------------------


def test_compare_assessments_reports_a_classification_difference():
    mimo = _with_classes()
    other = _with_classes()
    other["message_classes"] = [{"class": "闲话", "target": None}]

    status, differences = compare_assessments(mimo, other)

    assert status == "disagreed"
    assert "message_classes" in differences


def test_compare_assessments_is_silent_when_the_classification_matches():
    mimo = _with_classes()
    other = _with_classes()
    # Evidence-only fields differ; §2.3 lets them, so this is not a difference.
    other["message_classes"][0]["target"]["symbol"] = None

    status, differences = compare_assessments(mimo, other)

    assert "message_classes" not in differences
    assert status == "agreed"


def test_compare_assessments_says_nothing_when_neither_side_has_the_field():
    status, differences = compare_assessments(_base_payload(), _base_payload())

    assert status == "agreed"
    assert differences == []


def test_compare_assessments_flags_one_sided_presence():
    """An old prompt version against a new one is exactly the A/B case."""

    status, differences = compare_assessments(_with_classes(), _base_payload())

    assert status == "disagreed"
    assert differences == ["message_classes"]


# --------------------------------------------------------------------------
# §10 E1 / E2: the read-only page projection
# --------------------------------------------------------------------------


def _projected_row(tmp_path, payload, *, chat_id=77):
    from telegram_kol_research.models import RecognitionDecision
    from telegram_kol_research.web_queries import load_group_messages

    session_factory = create_session_factory(tmp_path / "projection.db")
    with session_factory() as session:
        message = RawMessage(
            chat_id=chat_id,
            message_id=5001,
            posted_at=datetime(2026, 9, 24, 11, 0, tzinfo=UTC),
            text="平掉，反手做多",
        )
        session.add(message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json=json.dumps(payload, ensure_ascii=False),
                agreement_status="pending",
                differences_json="[]",
                comparison_status="completed",
            )
        )
        session.commit()
    return load_group_messages(session_factory, chat_id=chat_id, limit=10)[0]


def test_the_page_projects_the_explicit_and_the_derived_list_side_by_side(tmp_path):
    row = _projected_row(tmp_path, _with_classes())

    assert row["message_classes"] == EXIT_AND_REVERSE_CLASSES
    assert [item["class"] for item in row["message_classes_derived"]] == [
        "仓位管理",
        "新策略",
    ]
    assert row["message_classes_agrees"] is True
    assert row["message_classes_violations"] == []
    # §10 E2: the card's own label is untouched in phase 1.
    assert row["recognition_result"] == "是策略"
    assert row["lifecycle_event_type"] == "exit_position"


def test_the_page_marks_a_disagreement_without_changing_the_old_fields(tmp_path):
    payload = _base_payload()
    payload["message_classes"] = [{"class": "闲话", "target": None}]

    row = _projected_row(tmp_path, payload)

    assert row["message_classes_agrees"] is False
    assert [item["class"] for item in row["message_classes_derived"]] == [
        "仓位管理",
        "新策略",
    ]
    assert row["recognition_result"] == "是策略"
    assert row["lifecycle_event_type"] == "exit_position"


def test_the_page_says_nothing_explicit_for_a_payload_from_the_old_prompt(tmp_path):
    row = _projected_row(tmp_path, _base_payload())

    assert row["message_classes"] is None
    assert row["message_classes_agrees"] is None
    assert row["message_classes_derived"] == [
        {
            "class": "仓位管理",
            "target": {
                "resolution": "exact",
                "lifecycle_id": 1319,
                "symbol": "ETH",
                "side": "short",
            },
        },
        {"class": "新策略", "target": None},
    ]


def test_the_card_template_shows_both_lists_and_leaves_the_main_chip_alone(tmp_path):
    """§10 E2: 阶段 1 先并排显示，卡片主标签不变。"""

    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path

    import telegram_kol_research

    row = _projected_row(tmp_path, _with_classes())
    row["message_classes"][0]["target"]["lifecycle_id"] = 1319
    environment = Environment(
        loader=FileSystemLoader(
            str(Path(telegram_kol_research.__file__).parent / "templates")
        ),
        autoescape=True,
    )
    rendered = environment.get_template("_messages.html").render(
        messages=[row],
        has_more=False,
        message_page_size=50,
        selected_chat_id=77,
        selected_group=None,
        search_text="",
        sender_name="",
        before_message_id=None,
        live_listener_enabled=False,
        monitor_status={"state": "idle"},
        live_listener_status_reason=None,
        live_listener_delegated=False,
        database_latest_message_at=None,
        database_stale_hours=None,
        refresh_mode_label="仅本地快照",
    )

    assert "显式分类" in rendered
    assert "推导分类" in rendered
    assert "data-message-class-explicit" in rendered
    assert "data-message-class-derived" in rendered
    # The pre-existing single-value derivation still drives the card label.
    assert 'data-message-ai-classification="management"' in rendered
    assert "data-message-classes-disagreement" not in rendered


def _render_card(row):
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    import telegram_kol_research

    environment = Environment(
        loader=FileSystemLoader(
            str(Path(telegram_kol_research.__file__).parent / "templates")
        ),
        autoescape=True,
    )
    return environment.get_template("_messages.html").render(
        messages=[row],
        has_more=False,
        message_page_size=50,
        selected_chat_id=77,
        selected_group=None,
        search_text="",
        sender_name="",
        before_message_id=None,
        live_listener_enabled=False,
        monitor_status={"state": "idle"},
        live_listener_status_reason=None,
        live_listener_delegated=False,
        database_latest_message_at=None,
        database_stale_hours=None,
        refresh_mode_label="仅本地快照",
    )


def test_the_card_carries_the_attributes_phase_2_filters_on(tmp_path):
    """阶段 2 的人工核准要能只看不一致的那些，所以判据得挂在卡片上。

    第一次测量里 18 条不一致散在 109 条中间，而一天就有 150-300 条；
    没有筛选入口，人工核准实际上做不动。
    """

    row = _projected_row(tmp_path, _with_classes())

    rendered = _render_card(row)

    # 这一条显式与推导一致，所以不该被「分类不一致」筛出来。
    assert 'data-message-classes-disagree="false"' in rendered
    assert 'data-message-classes-violation="false"' in rendered
    assert "仓位管理" in rendered
    assert 'data-message-class-resolutions="exact"' in rendered


def test_a_disagreeing_card_is_marked_for_the_filter(tmp_path):
    payload = _base_payload()
    payload["message_classes"] = [{"class": "闲话", "target": None}]

    rendered = _render_card(_projected_row(tmp_path, payload))

    assert 'data-message-classes-disagree="true"' in rendered
    assert 'data-message-class-names="闲话"' in rendered
    # 闲话 没有 target，所以 resolution 是空的 -- 「目标未知」筛选不该命中它。
    assert 'data-message-class-resolutions=""' in rendered


def test_every_filter_button_has_a_handler():
    """按钮和 JS 分支必须一一对应，少一边就是个点了没反应的按钮。"""

    from pathlib import Path

    import telegram_kol_research

    root = Path(telegram_kol_research.__file__).parent
    template = (root / "templates" / "_messages.html").read_text(encoding="utf-8")
    script = (root / "static" / "app.js").read_text(encoding="utf-8")

    import re

    buttons = set(re.findall(r'data-message-ai-filter="([a-z-]+)"', template))
    handled = set(re.findall(r"filterName === '([a-z-]+)'", script))

    assert {"classes-disagree", "classes-management", "classes-unknown",
            "classes-exact", "classes-violation"} <= buttons
    assert buttons - {"all"} == handled, (buttons - {"all"}) ^ handled
