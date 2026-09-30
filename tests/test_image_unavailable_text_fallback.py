"""2026-10-01 image-unavailable design
(docs/plans/2026-10-01-image-unavailable-text-fallback-design.md).

Replays the four production shapes of the 30-day inventory:

* raw 20102 (颜驰, text + an image whose live download timed out): the first
  pass refused the whole message five times without sending the text. Now the
  text is judged alone, once, and the result is marked ``text+image_missing``.
* raw 18368 / 20025 (比特币飞扬, image only, file missing): five blind retries,
  then ``failed``. Now terminal ``media_unavailable_waiting`` on the first
  attempt, re-queued by the ingest once the file is downloaded.
* raw 17301 (陈哥, the download never succeeded and the reconcile checkpoint
  moved on): the image left the retry set for good. Now a message posted in
  the last two hours stays in it.

Plus the execution gate (option 乙): judged without its image, a result may only
place an order whose price is in this message's own text.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.authoritative_recognition import (
    _alert_recognition_not_applied,
    _load_current_mimo_evidence_result,
    process_authoritative_message,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.image_missing_price_gate import assess_image_missing_prices
from telegram_kol_research.message_evidence import (
    INPUT_DEGRADATION_KEY,
    normalize_mimo_evidence,
)
from telegram_kol_research.message_processing_worker import (
    TerminalAuthoritativeProcessingFailed,
    process_message_job,
)
from telegram_kol_research.models import (
    MediaAsset,
    MessageEvidenceVersion,
    MessageProcessingJob,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.recognition_experiments import (
    IMAGE_UNAVAILABLE_ERROR,
    is_image_missing_input_kind,
    run_mimo_authoritative_for_message,
)
from telegram_kol_research.recognition_failure_attribution import (
    ALERTED_REASONS,
    IMAGE_MISSING_PRICE_NOT_IN_TEXT,
    MEDIA_UNAVAILABLE_WAITING,
)
from telegram_kol_research.telegram_live_listener import (
    ORPHAN_MEDIA_RETRY_WINDOW,
    _load_orphan_media_message_ids,
    _load_repaired_waiting_raw_ids,
    _enqueue_repaired_media_messages,
    _retry_live_media_download,
)

_PATCH = "telegram_kol_research.recognition_experiments._call_mimo_direct_model"
YAN_CHI_CHAT = -1003942765613
FEIYANG_CHAT = -1002960443256
CHEN_CHAT = -1002337721508
#: raw 20102, production text (truncated after the sentences that matter).
TEXT_20102 = (
    "这笔跟上的，慢慢832-828附近做止盈！ 成本也可以先防守看看！ "
    "给机会上去的话，依旧不变，任何的851突破后，盘整跌回都可以继续做空。"
)


def _model():
    return AiModelConfig(
        id="model-a",
        label="model-a",
        base_url="https://model-a.example/v1",
        api_key="key",
        model="model-a",
        supports_text=True,
        supports_image=True,
    )


def _chatter(**overrides):
    payload = {
        "recognition_result": "非策略",
        "reason": "闲聊",
        "strategy": None,
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "input_reading": {"observed_text": "x", "image_quality": "none"},
        "confidence": 0.9,
        "message_classes": [{"class": "闲话", "target": None}],
    }
    payload.update(overrides)
    return payload


def _seed(tmp_path, *, chat_id, message_id, text, images, posted_at=None):
    """``images``: list of (local_path, write_file) pairs."""

    session_factory = create_session_factory(tmp_path / "research.db")
    media_root = tmp_path / "media"
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            posted_at=posted_at or datetime(2026, 9, 30, 17, 48, 37, tzinfo=UTC),
        )
        session.add(raw)
        session.flush()
        for local_path, write_file in images:
            if write_file:
                path = media_root / local_path
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (4, 4)).save(path, format="JPEG")
            session.add(
                MediaAsset(raw_message_id=raw.id, kind="messagemediaphoto", local_path=local_path)
            )
        session.commit()
        raw_id = raw.id
    return session_factory, raw_id, media_root


def _capture(monkeypatch, payload):
    calls: list[dict] = []

    def fake_call(**kwargs):
        # Build the request while the caller's session still owns the rows.
        calls.append({**kwargs, "image_parts": _sent_image_parts(kwargs)})
        return payload

    monkeypatch.setattr(_PATCH, fake_call)
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments.resolve_authoritative_chain",
        lambda config: [_model()],
    )
    return calls


def _sent_image_parts(call) -> int:
    from telegram_kol_research.recognition_experiments import _build_mimo_payload

    request = _build_mimo_payload(
        raw_message=call["raw_message"],
        media_assets=call["media_assets"],
        model="model-a",
        media_root=call["media_root"],
    )
    content = request["messages"][1]["content"]
    if isinstance(content, str):
        return 0
    return sum(1 for part in content if part.get("type") == "image_url")


# ---------------------------------------------------------------------------
# first pass input (design §4.1)
# ---------------------------------------------------------------------------


def test_20102_text_is_judged_once_without_the_missing_image(tmp_path, monkeypatch):
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=YAN_CHI_CHAT,
        message_id=183,
        text=TEXT_20102,
        images=[(f"{YAN_CHI_CHAT}/183.jpg", False)],
    )
    calls = _capture(monkeypatch, _chatter())

    result = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=media_root,
    )

    assert len(calls) == 1
    assert result.error_message is None
    assert result.input_kind == "text+image_missing"
    assert is_image_missing_input_kind(result.input_kind)
    assert calls[0]["image_parts"] == 0
    with session_factory() as session:
        asset_id = session.query(MediaAsset.id).scalar()
    assert result.payload[INPUT_DEGRADATION_KEY] == {
        "image_missing": True,
        "missing_image_asset_ids": [asset_id],
    }
    # Nothing tells the model the image existed (design Q2).
    assert "Attached image sequence" not in json.dumps(calls[0]["raw_message"].text)


def test_only_the_readable_image_is_sent_when_one_of_two_is_missing(
    tmp_path, monkeypatch
):
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=YAN_CHI_CHAT,
        message_id=184,
        text="BTC 看图",
        images=[("g/ok.jpg", True), ("g/missing.jpg", False)],
    )
    calls = _capture(monkeypatch, _chatter())

    result = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=media_root,
    )

    assert result.input_kind == "text+image_missing"
    assert calls[0]["image_parts"] == 1


def test_image_only_message_with_its_image_missing_is_still_refused(
    tmp_path, monkeypatch
):
    # 18368 / 20025 shape: no text to fall back on.
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=FEIYANG_CHAT,
        message_id=5196,
        text=None,
        images=[(f"{FEIYANG_CHAT}/5196.jpg", False)],
    )
    calls = _capture(monkeypatch, _chatter())

    result = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=media_root,
    )

    assert calls == []
    assert result.error_message == IMAGE_UNAVAILABLE_ERROR
    assert result.input_kind == "image"


def test_readable_image_keeps_the_old_input_kind_and_no_marker(tmp_path, monkeypatch):
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=YAN_CHI_CHAT,
        message_id=185,
        text="BTC 看图",
        images=[("g/ok.jpg", True)],
    )
    calls = _capture(monkeypatch, _chatter())

    result = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=media_root,
    )

    assert result.input_kind == "text+image"
    assert INPUT_DEGRADATION_KEY not in result.payload
    assert calls[0]["image_parts"] == 1


def test_the_marker_survives_the_evidence_round_trip(tmp_path, monkeypatch):
    payload = _chatter()
    payload[INPUT_DEGRADATION_KEY] = {"image_missing": True, "missing_image_asset_ids": [7]}
    status, _conf, _text, _images, normalized = normalize_mimo_evidence(
        payload, input_kind="text+image_missing", error_message=None
    )
    # Stays "completed" so the replay / recovery path can reuse it.
    assert status == "completed"
    assert normalized[INPUT_DEGRADATION_KEY]["missing_image_asset_ids"] == [7]

    session_factory = create_session_factory(tmp_path / "evidence.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=1, message_id=1, text="x")
        session.add(raw)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=raw.id,
                version=1,
                input_fingerprint="sha256:x",
                extraction_status="completed",
                model="model-a",
                confidence=0.9,
                text_evidence_json="{}",
                image_evidence_json=json.dumps({"images": []}),
                normalized_evidence_json=json.dumps(normalized, ensure_ascii=False),
                prompt_versions_json="{}",
            )
        )
        session.commit()
        raw_id = raw.id

    mimo, _row = _load_current_mimo_evidence_result(session_factory, raw_id)
    assert mimo.input_kind == "text+image_missing"
    assert mimo.payload[INPUT_DEGRADATION_KEY]["image_missing"] is True


# ---------------------------------------------------------------------------
# execution gate, option 乙 (design §4.3)
# ---------------------------------------------------------------------------


def _entry(entry, stop, take_profit=None):
    return {
        "recognition_result": "是策略",
        "strategy": {
            "symbol": "ETH",
            "side": "long",
            "entry": entry,
            "stop_loss": stop,
            "take_profit": take_profit,
        },
        "lifecycle_event": {"event_type": "none"},
    }


def test_gate_passes_an_entry_whose_prices_are_all_in_the_text():
    text = "ETH 3120 附近多，止损 3040，目标 3200"
    assert assess_image_missing_prices(text, _entry("3120", "3040", "3200")) is None


def test_gate_refuses_an_entry_whose_stop_is_not_in_the_text():
    refusal = assess_image_missing_prices("ETH 3120 附近多", _entry("3120", "3040"))
    assert refusal is not None
    assert refusal.field == "strategy.stop_loss"


def test_gate_does_not_require_the_take_profit_of_an_entry():
    assert (
        assess_image_missing_prices("ETH 3120 多 止损3040", _entry("3120", "3040", "3300"))
        is None
    )


def test_gate_reads_ranges_and_market_entries():
    text = "ETH 市价进场，1730附近补，止损 1690"
    assert assess_image_missing_prices(text, _entry("市价进场/1730附近", "1690")) is None
    text = "BTC 84500-84800 空 止损 85200"
    assert assess_image_missing_prices(text, _entry("84500-84800", "85200")) is None
    assert assess_image_missing_prices(text, _entry("84500-84900", "85200")) is not None


def test_gate_does_not_count_a_contact_number_as_a_price():
    text = "ETH 3120 多 QQ:158241758"
    refusal = assess_image_missing_prices(text, _entry("3120", "158241758"))
    assert refusal is not None


def test_gate_20102_take_profit_from_the_text_passes():
    payload = {
        "recognition_result": "非策略",
        "strategy": None,
        "lifecycle_event": {
            "event_type": "position_update",
            "management_action": "adjust_take_profit",
            "take_profit": "832-828",
        },
    }
    assert assess_image_missing_prices(TEXT_20102, payload) is None


def test_gate_refuses_a_take_profit_the_text_never_named():
    payload = {
        "lifecycle_event": {
            "event_type": "position_update",
            "management_action": "adjust_take_profit",
            "take_profit": "815",
        },
    }
    refusal = assess_image_missing_prices(TEXT_20102, payload)
    assert refusal is not None and refusal.field == "lifecycle_event.take_profit"


def test_gate_leaves_priceless_management_alone():
    exit_payload = {
        "lifecycle_event": {"event_type": "exit_position", "exit_price": "86100"},
        "instructions": [{"kind": "full_exit", "parameters": {"price": "86100"}}],
    }
    assert assess_image_missing_prices("全部平仓出局", exit_payload) is None
    break_even = {
        "lifecycle_event": {
            "event_type": "position_update",
            "management_action": "move_stop_to_protect",
            "stop_loss": "3120",
        },
    }
    assert assess_image_missing_prices("止损移到成本，保护好", break_even) is None


def test_gate_checks_entry_instructions_and_price_parameters():
    payload = {
        "instructions": [
            {"kind": "entry", "strategy": {"entry": "3120", "stop_loss": "3040"}},
        ]
    }
    assert assess_image_missing_prices("ETH 3120 多", payload).field == (
        "instructions[0].strategy.stop_loss"
    )
    payload = {
        "instructions": [
            {"kind": "risk_update", "parameters": {"stop_loss": "3050", "fraction": "0.5"}},
        ]
    }
    assert assess_image_missing_prices("止损改到 3050", payload) is None
    assert assess_image_missing_prices("止损上移", payload) is not None


# ---------------------------------------------------------------------------
# assessment and job (design §4.3 / §4.4)
# ---------------------------------------------------------------------------


def _process(session_factory, raw_id, media_root, executor_calls):
    return process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=media_root,
        auto_trade_executor=executor_calls.append,
    )


def _quiet_wakeups(monkeypatch):
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition._run_entry_assembly_wakeups",
        lambda *args, **kwargs: None,
    )


def test_image_only_missing_ends_terminal_waiting_and_is_not_retried(
    tmp_path, monkeypatch
):
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=FEIYANG_CHAT,
        message_id=5196,
        text=None,
        images=[(f"{FEIYANG_CHAT}/5196.jpg", False)],
    )
    calls = _capture(monkeypatch, _chatter())
    _quiet_wakeups(monkeypatch)
    executor_calls: list[int] = []

    result = _process(session_factory, raw_id, media_root, executor_calls)

    assert calls == []
    assert result.assessment.agreement_status == "authoritative_failed"
    assert result.assessment.terminal_failure_reason == MEDIA_UNAVAILABLE_WAITING
    assert result.automation == {"status": "skipped", "reason": MEDIA_UNAVAILABLE_WAITING}
    assert executor_calls == []

    with pytest.raises(TerminalAuthoritativeProcessingFailed) as raised:
        asyncio.run(
            process_message_job(
                session_factory,
                raw_message_id=raw_id,
                authoritative_processor=lambda rid: _process(
                    session_factory, rid, media_root, []
                ),
                retry_authoritative_failure=False,
            )
        )
    assert raised.value.queue_reason == (
        f"terminal_authoritative_failure:{MEDIA_UNAVAILABLE_WAITING}"
    )


def test_20102_job_completes_on_the_first_attempt(tmp_path, monkeypatch):
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=YAN_CHI_CHAT,
        message_id=183,
        text=TEXT_20102,
        images=[(f"{YAN_CHI_CHAT}/183.jpg", False)],
    )
    calls = _capture(monkeypatch, _chatter())
    _quiet_wakeups(monkeypatch)

    result = asyncio.run(
        process_message_job(
            session_factory,
            raw_message_id=raw_id,
            authoritative_processor=lambda rid: _process(
                session_factory, rid, media_root, []
            ),
            retry_authoritative_failure=False,
        )
    )

    assert len(calls) == 1
    assert result.assessment.agreement_status != "authoritative_failed"
    with session_factory() as session:
        run = session.query(MimoRecognitionRun).one()
        decision = session.query(RecognitionDecision).one()
    assert run.status == "completed"
    assert run.input_kind == "text+image_missing"
    assert decision.input_kind == "text+image_missing"


def test_entry_judged_without_its_image_and_a_stop_not_in_text_is_refused(
    tmp_path, monkeypatch
):
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=CHEN_CHAT,
        message_id=900,
        text="ETH 3120 附近多，止损看图",
        images=[(f"{CHEN_CHAT}/900.jpg", False)],
    )
    payload = _chatter(
        recognition_result="是策略",
        strategy={
            "symbol": "ETH",
            "side": "long",
            "entry": "3120",
            "stop_loss": "3040",
            "take_profit": None,
            "leverage": None,
            "order_type": "limit",
        },
        message_classes=[{"class": "新策略", "target": None}],
    )
    _capture(monkeypatch, payload)
    _quiet_wakeups(monkeypatch)
    executor_calls: list[int] = []

    result = _process(session_factory, raw_id, media_root, executor_calls)

    assert result.assessment.agreement_status == "authoritative_failed"
    assert (
        result.assessment.terminal_failure_reason == IMAGE_MISSING_PRICE_NOT_IN_TEXT
    )
    assert result.automation == {
        "status": "skipped",
        "reason": IMAGE_MISSING_PRICE_NOT_IN_TEXT,
    }
    assert executor_calls == []
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
    # The recognised strategy is still on record for the Web card.
    assert json.loads(decision.authoritative_payload_json)["strategy"]["stop_loss"] == "3040"


def test_both_new_reasons_are_alerted_in_trading_groups(tmp_path):
    assert MEDIA_UNAVAILABLE_WAITING in ALERTED_REASONS
    assert IMAGE_MISSING_PRICE_NOT_IN_TEXT in ALERTED_REASONS
    session_factory = create_session_factory(tmp_path / "alert.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=CHEN_CHAT, message_id=1, text="x")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    captured: list[dict] = []
    alerted = _alert_recognition_not_applied(
        session_factory,
        raw_message_id=raw_id,
        automation={"status": "skipped", "reason": MEDIA_UNAVAILABLE_WAITING},
        group_trading_mode_provider=lambda _chat: "auto_trade",
        capture=lambda **kwargs: captured.append(kwargs),
    )
    assert alerted == MEDIA_UNAVAILABLE_WAITING
    assert captured[0]["reason_code"] == MEDIA_UNAVAILABLE_WAITING


# ---------------------------------------------------------------------------
# ingest repair (design §4.5)
# ---------------------------------------------------------------------------


def test_17301_shape_stays_in_the_retry_set_within_two_hours(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime.now(UTC)
    with session_factory() as session:
        for message_id, posted_at in (
            (100, now - timedelta(hours=1)),
            (101, now - ORPHAN_MEDIA_RETRY_WINDOW - timedelta(minutes=30)),
        ):
            raw = RawMessage(
                chat_id=CHEN_CHAT, message_id=message_id, text="x", posted_at=posted_at
            )
            session.add(raw)
            session.flush()
            session.add(MediaAsset(raw_message_id=raw.id, kind="photo", local_path=None))
        session.commit()

    # The checkpoint has moved far past both (replay floor 195).
    without_window = _load_orphan_media_message_ids(
        session_factory,
        dialog_id=CHEN_CHAT,
        replay_floor=195,
        media_root=tmp_path / "media",
    )
    with_window = _load_orphan_media_message_ids(
        session_factory,
        dialog_id=CHEN_CHAT,
        replay_floor=195,
        media_root=tmp_path / "media",
        recent_since=now - ORPHAN_MEDIA_RETRY_WINDOW,
    )

    assert without_window == set()
    assert with_window == {100}


def _seed_decided(tmp_path, *, reason, file_present):
    session_factory, raw_id, media_root = _seed(
        tmp_path,
        chat_id=FEIYANG_CHAT,
        message_id=5196,
        text=None,
        images=[(f"{FEIYANG_CHAT}/5196.jpg", file_present)],
    )
    with session_factory() as session:
        session.add(
            RecognitionDecision(
                raw_message_id=raw_id,
                input_kind="image",
                authoritative_model="model-a",
                authoritative_status="识别失败",
                authoritative_payload_json="{}",
                agreement_status="authoritative_failed",
                differences_json="[]",
                automation_status="skipped",
                automation_reason=reason,
            )
        )
        session.add(
            MessageProcessingJob(
                raw_message_id=raw_id,
                chat_id=FEIYANG_CHAT,
                status="succeeded",
                attempt_count=1,
                last_reason=f"terminal_authoritative_failure:{reason}",
            )
        )
        session.commit()
    return session_factory, raw_id, media_root


def test_repaired_image_requeues_a_waiting_message(tmp_path):
    session_factory, raw_id, media_root = _seed_decided(
        tmp_path, reason=MEDIA_UNAVAILABLE_WAITING, file_present=True
    )

    queued = asyncio.run(
        _enqueue_repaired_media_messages(
            session_factory,
            dialog_id=FEIYANG_CHAT,
            message_ids={5196},
            media_root=media_root,
        )
    )

    assert queued == [raw_id]
    with session_factory() as session:
        job = session.query(MessageProcessingJob).one()
    assert job.status == "pending"
    assert job.attempt_count == 0
    assert job.last_reason == "media_repaired_enqueued"


def test_waiting_message_without_its_file_yet_is_not_requeued(tmp_path):
    session_factory, _raw_id, media_root = _seed_decided(
        tmp_path, reason=MEDIA_UNAVAILABLE_WAITING, file_present=False
    )

    assert (
        _load_repaired_waiting_raw_ids(
            session_factory,
            dialog_id=FEIYANG_CHAT,
            message_ids={5196},
            media_root=media_root,
        )
        == []
    )


def test_a_message_already_judged_from_text_is_not_rerun(tmp_path):
    # Design Q3: only the waiting reason is re-queued.
    session_factory, _raw_id, media_root = _seed_decided(
        tmp_path, reason="mimo_no_action", file_present=True
    )

    assert (
        _load_repaired_waiting_raw_ids(
            session_factory,
            dialog_id=FEIYANG_CHAT,
            message_ids={5196},
            media_root=media_root,
        )
        == []
    )


class _RetryClient:
    def __init__(self, media_root: Path, *, succeed_on: int):
        self.media_root = media_root
        self.succeed_on = succeed_on
        self.fetches = 0

    async def get_messages(self, chat_id, ids):
        self.fetches += 1
        return object()


def test_live_retry_downloads_records_and_requeues(tmp_path, monkeypatch):
    session_factory, raw_id, media_root = _seed_decided(
        tmp_path, reason=MEDIA_UNAVAILABLE_WAITING, file_present=False
    )
    # The stored path is empty after a failed live download.
    with session_factory() as session:
        session.query(MediaAsset).update({"local_path": None})
        session.commit()
    client = _RetryClient(media_root, succeed_on=2)
    downloads: list[int] = []

    async def fake_download(client_arg, *, dialog_id, message, media_root):
        downloads.append(dialog_id)
        if len(downloads) < client.succeed_on:
            return None
        path = Path(media_root) / f"{dialog_id}" / "5196.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image

        Image.new("RGB", (4, 4)).save(path, format="JPEG")
        return f"{dialog_id}/5196.jpg"

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener._download_media_if_present",
        fake_download,
    )

    path = asyncio.run(
        _retry_live_media_download(
            client,
            session_factory=session_factory,
            chat_id=FEIYANG_CHAT,
            message_id=5196,
            media_root=media_root,
            delays=(0, 0, 0),
        )
    )

    assert path == f"{FEIYANG_CHAT}/5196.jpg"
    assert client.fetches == 2
    with session_factory() as session:
        asset = session.query(MediaAsset).one()
        job = session.query(MessageProcessingJob).one()
    assert asset.local_path == f"{FEIYANG_CHAT}/5196.jpg"
    assert job.status == "pending"
    assert job.last_reason == "media_repaired_enqueued"


def test_live_retry_gives_up_quietly_after_its_delays(tmp_path, monkeypatch):
    session_factory, _raw_id, media_root = _seed_decided(
        tmp_path, reason=MEDIA_UNAVAILABLE_WAITING, file_present=False
    )

    async def never(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener._download_media_if_present",
        never,
    )
    client = _RetryClient(media_root, succeed_on=99)

    path = asyncio.run(
        _retry_live_media_download(
            client,
            session_factory=session_factory,
            chat_id=FEIYANG_CHAT,
            message_id=5196,
            media_root=media_root,
            delays=(0, 0),
        )
    )

    assert path is None
    assert client.fetches == 2
