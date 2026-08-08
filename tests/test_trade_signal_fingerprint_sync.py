from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

import telegram_kol_research.trade_signals as trade_signals_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trade_signals import load_trade_signal
from telegram_kol_research.trade_signals import (
    synchronize_pending_entry_assembly_evidence,
)
from telegram_kol_research.trade_signals import TradeSignalFingerprintSyncError


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
OLD_FINGERPRINT = "a" * 64
FINAL_FINGERPRINT = "b" * 64


def _payload() -> dict[str, object]:
    evidence = {
        "assembly_id": 2,
        "strategy_instance_id": "strategy-1",
        "assembly_fingerprint": OLD_FINGERPRINT,
    }
    return {
        "entry_preamble_assembly": deepcopy(evidence),
        "deepcoin_order_draft": {
            "symbol": "BTCUSDT",
            "entry_preamble_assembly": deepcopy(evidence),
        },
    }


def _enqueue(session_factory, *, payload: dict[str, object] | None = None):
    return enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload=payload if payload is not None else _payload(),
        strategy_instance_id="strategy-1",
    )


def _synchronize(session_factory, signal, **overrides):
    arguments = {
        "signal_id": signal.id,
        "strategy_instance_id": "strategy-1",
        "expected_payload": signal.payload,
        "expected_fingerprint": OLD_FINGERPRINT,
        "finalized_evidence": {
            "assembly_id": 2,
            "strategy_instance_id": "strategy-1",
            "assembly_fingerprint": FINAL_FINGERPRINT,
        },
        "synchronized_at": NOW,
    }
    arguments.update(overrides)
    return synchronize_pending_entry_assembly_evidence(session_factory, **arguments)


def test_synchronize_pending_entry_assembly_evidence_updates_both_copies(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = _enqueue(session_factory)

    updated = _synchronize(session_factory, signal)

    assert updated.status == "pending"
    assert (
        updated.payload["entry_preamble_assembly"]["assembly_fingerprint"]
        == FINAL_FINGERPRINT
    )
    assert (
        updated.payload["deepcoin_order_draft"]["entry_preamble_assembly"]
        ["assembly_fingerprint"]
        == FINAL_FINGERPRINT
    )
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row is not None
        assert row.updated_at == NOW.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("non_pending", "entry_assembly_signal_cas_failed"),
        ("strategy_mismatch", "entry_assembly_signal_identity_mismatch"),
        ("payload_drift", "entry_assembly_signal_cas_failed"),
        ("absent_draft", "entry_assembly_signal_draft_invalid"),
        ("malformed_draft", "entry_assembly_signal_draft_invalid"),
        ("wrong_top_fingerprint", "entry_assembly_signal_fingerprint_mismatch"),
        ("wrong_nested_fingerprint", "entry_assembly_signal_fingerprint_mismatch"),
    ],
)
def test_synchronize_pending_entry_assembly_evidence_fails_without_mutation(
    tmp_path,
    case,
    expected_error,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = _payload()
    if case == "absent_draft":
        payload.pop("deepcoin_order_draft")
    elif case == "malformed_draft":
        payload["deepcoin_order_draft"] = "invalid"
    elif case == "wrong_top_fingerprint":
        payload["entry_preamble_assembly"]["assembly_fingerprint"] = "c" * 64
    elif case == "wrong_nested_fingerprint":
        payload["deepcoin_order_draft"]["entry_preamble_assembly"][
            "assembly_fingerprint"
        ] = "c" * 64
    signal = _enqueue(session_factory, payload=payload)
    original_payload = deepcopy(signal.payload)
    kwargs = {}

    if case == "non_pending":
        with session_factory() as session:
            row = session.get(TradeSignal, signal.id)
            assert row is not None
            row.status = "submitted"
            session.commit()
    elif case == "strategy_mismatch":
        kwargs["strategy_instance_id"] = "strategy-other"
    elif case == "payload_drift":
        with session_factory() as session:
            row = session.get(TradeSignal, signal.id)
            assert row is not None
            row.payload_json = row.payload_json.replace("BTCUSDT", "ETHUSDT")
            session.commit()

    with pytest.raises(TradeSignalFingerprintSyncError, match=expected_error):
        _synchronize(session_factory, signal, **kwargs)

    reloaded = load_trade_signal(session_factory, signal.id)
    if case == "payload_drift":
        assert reloaded.payload["deepcoin_order_draft"]["symbol"] == "ETHUSDT"
        expected = deepcopy(original_payload)
        expected["deepcoin_order_draft"]["symbol"] = "ETHUSDT"
        assert reloaded.payload == expected
    else:
        assert reloaded.payload == original_payload


@pytest.mark.parametrize(
    ("finalized_evidence", "expected_error"),
    [
        (
            {
                "assembly_id": 999,
                "strategy_instance_id": "strategy-1",
                "assembly_fingerprint": FINAL_FINGERPRINT,
            },
            "entry_assembly_signal_identity_mismatch",
        ),
        (
            {
                "assembly_id": 2,
                "strategy_instance_id": "strategy-other",
                "assembly_fingerprint": FINAL_FINGERPRINT,
            },
            "entry_assembly_signal_identity_mismatch",
        ),
        (
            {
                "assembly_id": 2,
                "strategy_instance_id": "strategy-1",
                "assembly_fingerprint": "",
            },
            "entry_assembly_signal_final_evidence_invalid",
        ),
        (
            {
                "assembly_id": 2,
                "strategy_instance_id": "strategy-1",
                "assembly_fingerprint": "z" * 64,
            },
            "entry_assembly_signal_final_evidence_invalid",
        ),
        (
            {
                "assembly_id": 2,
                "strategy_instance_id": "strategy-1",
                "assembly_fingerprint": "b" * 63,
            },
            "entry_assembly_signal_final_evidence_invalid",
        ),
    ],
)
def test_synchronize_rejects_invalid_final_identity_without_mutation(
    tmp_path,
    finalized_evidence,
    expected_error,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = _enqueue(session_factory)
    original_payload = deepcopy(signal.payload)

    with pytest.raises(TradeSignalFingerprintSyncError, match=f"^{expected_error}$"):
        _synchronize(
            session_factory,
            signal,
            finalized_evidence=finalized_evidence,
        )

    assert load_trade_signal(session_factory, signal.id).payload == original_payload


@pytest.mark.parametrize(
    ("field", "divergent_value"),
    [
        ("assembly_id", 999),
        ("strategy_instance_id", "strategy-other"),
    ],
)
def test_synchronize_rejects_divergent_old_identity_without_mutation(
    tmp_path,
    field,
    divergent_value,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = _payload()
    payload["deepcoin_order_draft"]["entry_preamble_assembly"][field] = (
        divergent_value
    )
    signal = _enqueue(session_factory, payload=payload)
    original_payload = deepcopy(signal.payload)

    with pytest.raises(
        TradeSignalFingerprintSyncError,
        match="^entry_assembly_signal_identity_mismatch$",
    ):
        _synchronize(session_factory, signal)

    assert load_trade_signal(session_factory, signal.id).payload == original_payload


def test_synchronize_normalizes_uppercase_hex_fingerprints(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = _payload()
    payload["entry_preamble_assembly"]["assembly_fingerprint"] = (
        OLD_FINGERPRINT.upper()
    )
    payload["deepcoin_order_draft"]["entry_preamble_assembly"][
        "assembly_fingerprint"
    ] = OLD_FINGERPRINT.upper()
    signal = _enqueue(session_factory, payload=payload)

    updated = _synchronize(
        session_factory,
        signal,
        expected_fingerprint=OLD_FINGERPRINT.upper(),
        finalized_evidence={
            "assembly_id": 2,
            "strategy_instance_id": "strategy-1",
            "assembly_fingerprint": FINAL_FINGERPRINT.upper(),
        },
    )

    assert (
        updated.payload["entry_preamble_assembly"]["assembly_fingerprint"]
        == FINAL_FINGERPRINT
    )
    assert (
        updated.payload["deepcoin_order_draft"]["entry_preamble_assembly"]
        ["assembly_fingerprint"]
        == FINAL_FINGERPRINT
    )


@pytest.mark.parametrize("invalid_assembly_id", [True, 0, "2"])
def test_synchronize_rejects_non_positive_integer_final_assembly_id(
    tmp_path,
    invalid_assembly_id,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = _payload()
    if invalid_assembly_id is True:
        payload["entry_preamble_assembly"]["assembly_id"] = 1
        payload["deepcoin_order_draft"]["entry_preamble_assembly"][
            "assembly_id"
        ] = 1
    signal = _enqueue(session_factory, payload=payload)
    original_payload = deepcopy(signal.payload)

    with pytest.raises(
        TradeSignalFingerprintSyncError,
        match="^entry_assembly_signal_identity_mismatch$",
    ):
        _synchronize(
            session_factory,
            signal,
            finalized_evidence={
                "assembly_id": invalid_assembly_id,
                "strategy_instance_id": "strategy-1",
                "assembly_fingerprint": FINAL_FINGERPRINT,
            },
        )

    assert load_trade_signal(session_factory, signal.id).payload == original_payload


@pytest.mark.parametrize("invalid_assembly_id", [True, 0, "2"])
def test_synchronize_rejects_non_positive_integer_nested_assembly_id(
    tmp_path,
    invalid_assembly_id,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = _payload()
    final_assembly_id = 2
    if invalid_assembly_id is True:
        payload["entry_preamble_assembly"]["assembly_id"] = 1
        final_assembly_id = 1
    payload["deepcoin_order_draft"]["entry_preamble_assembly"]["assembly_id"] = (
        invalid_assembly_id
    )
    signal = _enqueue(session_factory, payload=payload)
    original_payload = deepcopy(signal.payload)

    with pytest.raises(
        TradeSignalFingerprintSyncError,
        match="^entry_assembly_signal_identity_mismatch$",
    ):
        _synchronize(
            session_factory,
            signal,
            finalized_evidence={
                "assembly_id": final_assembly_id,
                "strategy_instance_id": "strategy-1",
                "assembly_fingerprint": FINAL_FINGERPRINT,
            },
        )

    assert load_trade_signal(session_factory, signal.id).payload == original_payload


def test_synchronize_rejects_post_cas_payload_drift_retaining_fingerprints(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = _enqueue(session_factory)
    real_load = trade_signals_module.load_trade_signal

    def load_with_post_cas_drift(factory, signal_id):
        record = real_load(factory, signal_id)
        record.payload["deepcoin_order_draft"]["symbol"] = "ETHUSDT"
        return record

    monkeypatch.setattr(
        trade_signals_module,
        "load_trade_signal",
        load_with_post_cas_drift,
    )

    with pytest.raises(
        TradeSignalFingerprintSyncError,
        match="^entry_assembly_signal_reload_validation_failed$",
    ):
        _synchronize(session_factory, signal)
