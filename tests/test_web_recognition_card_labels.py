"""The message card names the model that ran and the state of the second pass.

Two user-visible claims are pinned here:

* the authoritative heading is stage-named, not ``MiMo``-named, and carries the
  display name of whichever model actually produced the result;
* the contextual card always says whether the second pass ran, and when it did,
  which trigger signal paid for it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.web_app import create_web_app


STARTED_AT = datetime(2026, 9, 15, 2, 0)


def _card(body: str, raw_message_id: int) -> str:
    marker = f'id="message-{raw_message_id}"'
    marker_index = body.index(marker)
    start = body.rfind("<article", 0, marker_index)
    next_card = body.find('\n      <article\n        class="message-card', marker_index)
    return body[start : next_card if next_card >= 0 else len(body)]


def _add_recognised_message(
    session,
    *,
    message_id: int,
    text: str,
    model: str,
    gate: dict | None,
) -> int:
    raw = RawMessage(chat_id=88, message_id=message_id, sender_name="KOL", text=text)
    session.add(raw)
    session.flush()
    session.add(
        MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v1_authoritative",
            contract_version="v1",
            model=model,
            input_kind="text",
            input_fingerprint=f"sha256:{message_id}",
            prompt_versions_json="{}",
            status="completed",
            attempt_count=1,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            started_at=STARTED_AT,
            completed_at=STARTED_AT + timedelta(milliseconds=40),
        )
    )
    session.add(
        RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model=model,
            authoritative_status="是策略",
            authoritative_payload_json=json.dumps(
                {"recognition_result": "是策略", "summary": text},
                ensure_ascii=False,
            ),
            agreement_status="pending",
            differences_json="[]",
            context_resolution_gate_json=(
                None if gate is None else json.dumps(gate, ensure_ascii=False)
            ),
        )
    )
    return int(raw.id)


def _build(tmp_path):
    database_path = tmp_path / "recognition-card-labels.db"
    session_factory = create_session_factory(database_path)
    ids: dict[str, int] = {}
    with session_factory() as session:
        ids["executed"] = _add_recognised_message(
            session,
            message_id=901,
            text="止损改为 1940，之前那单取消",
            model="gpt-5.6-luna",
            gate={
                "outcome": "invoked",
                "triggers": ["revision_language", "cancellation_language"],
            },
        )
        session.add(
            ContextResolutionAttempt(
                raw_message_id=ids["executed"],
                context_fingerprint="sha256:ctx-901",
                model="gpt-5.6-luna",
                status="completed",
                invocation_triggers_json=json.dumps(
                    ["revision_language", "cancellation_language"]
                ),
                request_summary_json=json.dumps(
                    {"message_context": [{"message_id": 900}, {"message_id": 899}]}
                ),
                decision_json=json.dumps(
                    {
                        "decision": "same_strategy",
                        "confidence": 0.91,
                        "supporting_message_ids": [900],
                        "opposing_message_ids": [],
                    }
                ),
            )
        )
        ids["not_needed"] = _add_recognised_message(
            session,
            message_id=902,
            text="BTC 新多单，市价进",
            model="gpt-5.6-luna",
            gate={"outcome": "not_needed", "triggers": []},
        )
        ids["capped"] = _add_recognised_message(
            session,
            message_id=906,
            text="第二止盈位到了",
            model="gpt-5.6-luna",
            gate={"outcome": "invoked", "triggers": ["revision_language"]},
        )
        session.add(
            ContextResolutionAttempt(
                raw_message_id=ids["capped"],
                context_fingerprint="sha256:ctx-906",
                model="gpt-5.6-luna",
                status="reanalysis_capped",
                invocation_triggers_json=json.dumps(["revision_language"]),
                request_summary_json=json.dumps({"message_context": []}),
                decision_json=json.dumps(
                    {
                        "decision": "unresolved",
                        "confidence": 0.5,
                        "supporting_message_ids": [],
                        "opposing_message_ids": [],
                    }
                ),
            )
        )
        ids["unknown_model"] = _add_recognised_message(
            session,
            message_id=903,
            text="ETH 新空单，市价进",
            model="retired-model-7",
            gate={"outcome": "not_needed", "triggers": []},
        )
        session.commit()
    return database_path, ids


def test_authoritative_heading_names_the_stage_and_the_real_model(tmp_path):
    database_path, ids = _build(tmp_path)

    body = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    ).text
    executed = _card(body, ids["executed"])

    assert "权威识别结果" in executed
    assert "识别成功" in executed
    # The stage is model-agnostic now; the heading must not name one provider.
    assert "MiMo v1结果" not in body
    assert "MiMo v1回退结果" not in body
    assert "MiMo第一次识别" not in body
    assert "MiMo识别成功" not in body
    assert '<span class="mimo-runtime-model">gpt-5.6-luna</span>' in executed


def test_unknown_model_id_falls_back_to_the_raw_id(tmp_path):
    database_path, ids = _build(tmp_path)

    body = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    ).text
    unknown = _card(body, ids["unknown_model"])

    # A model that has left the configuration keeps its id rather than
    # rendering as an empty badge.
    assert '<span class="mimo-runtime-model">retired-model-7</span>' in unknown
    assert "模型 retired-model-7 · 合约" in unknown
    assert "retired-model-7（retired-model-7）" not in unknown


def test_configured_model_label_replaces_the_raw_id_in_the_heading(tmp_path):
    database_path = tmp_path / "labelled-model.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_id = _add_recognised_message(
            session,
            message_id=904,
            text="老消息",
            model="mimo-v2.5",
            gate=None,
        )
        session.commit()

    body = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    ).text
    card = _card(body, raw_id)

    assert '<span class="mimo-runtime-model">MiMo V2.5</span>' in card
    assert "模型 MiMo V2.5（mimo-v2.5）" in card


def test_executed_context_card_shows_state_and_chinese_trigger_chips(tmp_path):
    database_path, ids = _build(tmp_path)

    body = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    ).text
    executed = _card(body, ids["executed"])

    assert '<span class="context-exec-state is-completed">已执行 · 决策 same_strategy' in executed
    assert '<span class="context-trigger-chip">修改措辞</span>' in executed
    assert '<span class="context-trigger-chip">取消措辞</span>' in executed
    assert "上下文模型 gpt-5.6-luna" in executed
    # The reason has to survive collapsing the card.
    assert "🔗 已结合(2 条) · 触发：修改措辞 等 2 项" in executed
    assert (
        'data-message-context-triggers="revision_language,cancellation_language"'
        in executed
    )
    # The trigger chips are the first line of the body, not buried in details.
    assert executed.index("context-trigger-reasons") < executed.index(
        "上下文技术明细"
    )


def test_capped_context_card_says_the_reanalysis_ceiling_was_reached(tmp_path):
    """A capped message stops costing tokens, and the card says so."""

    database_path, ids = _build(tmp_path)

    body = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    ).text
    capped = _card(body, ids["capped"])

    assert (
        '<span class="context-exec-state is-reanalysis_capped">重分析已达上限'
        in capped
    )


def test_recognised_message_without_an_attempt_still_shows_why_it_did_not_run(
    tmp_path,
):
    database_path, ids = _build(tmp_path)

    body = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    ).text
    not_needed = _card(body, ids["not_needed"])

    assert "上下文二次判断" in not_needed
    assert (
        '<span class="context-exec-state is-not_needed">未执行：未命中触发条件'
        in not_needed
    )
    assert "无（未命中任何信号）" in not_needed
    assert 'data-message-context-triggers=""' in not_needed


def test_historical_message_without_a_gate_says_the_reason_was_not_recorded(
    tmp_path,
):
    database_path = tmp_path / "historical-gate.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_id = _add_recognised_message(
            session,
            message_id=905,
            text="历史消息",
            model="mimo-v2.5",
            gate=None,
        )
        session.commit()

    body = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/88/messages"
    ).text
    card = _card(body, raw_id)

    assert (
        '<span class="context-exec-state is-unknown">未执行（历史消息，未记录原因）'
        in card
    )


def test_stats_line_aggregates_trigger_reasons_client_side(tmp_path):
    database_path, _ = _build(tmp_path)
    client = TestClient(create_web_app(database_path=database_path))

    js = client.get("/static/app.js").text

    # The distribution is summed in the browser from the data attribute the
    # cards carry, and appended to the existing "上下文调用 N" figure.
    assert "data-message-context-triggers" not in js  # read via dataset, not a selector
    assert "messageContextTriggers" in js
    assert "function summarizeContextTriggers" in js
    assert "上下文调用 ${contextCalls}（${triggerSummary}）" in js
    assert "revision_language: '修改措辞'" in js
    assert "apparent_entry_may_be_revision: '疑似入场实为修改'" in js
