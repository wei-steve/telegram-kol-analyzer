from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.trading_settings import trading_settings_from_payload

NOW = datetime(2026, 9, 5, 17, tzinfo=UTC)


def check(**overrides):
    from telegram_kol_research import management_stop_price_gate as gate

    values = {
        "action": "adjust_stop_loss",
        "stop_mode": "explicit_price",
        "stop_price": "79000",
        "stop_price_source": "current_message_text",
        "current_message_text": "止损79000",
        "side": "long",
        "entry_prices": ["79519"],
        "instrument_id": "BTC-USDT-SWAP",
        "quote": {
            "instrument_id": "BTC-USDT-SWAP",
            "price": "80000",
            "price_field": "last",
            "observed_at": NOW.isoformat(),
        },
        "settings": trading_settings_from_payload({}),
        "now": NOW,
    }
    values.update(overrides)
    return gate.validate_management_stop(**values)


@pytest.mark.parametrize(
    "stop,side,reason",
    [
        ("158241758", "long", "management_stop_deviation_exceeded"),
        ("81000", "long", "management_stop_direction_invalid"),
        ("79000", "short", "management_stop_direction_invalid"),
        ("80000", "long", "management_stop_direction_invalid"),
        ("100", "long", "management_stop_deviation_exceeded"),
        ("NaN", "long", "management_stop_price_invalid"),
        ("Infinity", "long", "management_stop_price_invalid"),
    ],
)
def test_rejects_unsafe_explicit_stop(stop, side, reason):
    result = check(stop_price=stop, side=side)
    assert result.reason_code == reason
    assert result.evidence["reference_price_source"] == "current_market_last"
    assert result.evidence["entry_prices"] == ["79519"]


@pytest.mark.parametrize(
    "stop,side", [("79000", "long"), ("79900", "long"), ("81000", "short")]
)
def test_reasonable_and_profit_locking_explicit_stops_pass(stop, side):
    assert check(stop_price=stop, side=side).reason_code is None


@pytest.mark.parametrize(
    "action", ["partial_then_break_even", "move_stop_to_break_even"]
)
def test_implicit_target_action_cannot_choose_explicit_price(action):
    """Kept as defence in depth.

    Since 2026-09-21 a break-even instruction is not supposed to *reach* this
    branch: every explicit price it carries is disposed of before planning
    persists anything, and the batch stores the neutralised contract. This
    rule stays exactly as it was so that a break-even batch which somehow
    still names a price is refused rather than executed.
    """

    assert check(action=action).reason_code == "management_stop_action_conflict"


class _Batch:
    def __init__(self, *, contract_json, legs, intent="partial_then_break_even"):
        self.intent = intent
        self.management_contract_json = contract_json
        self.legs = legs
        self.id = 1
        self.raw_message_id = 1
        self.execution_binding_id = 1
        self.status = "executing"


class _Leg:
    def __init__(self, planned_tpsl):
        self.planned_tpsl = planned_tpsl
        self.avg_entry_price = "80436"


def test_a_neutralised_break_even_batch_passes_the_execution_recheck():
    """The batch a disposed price produces must not conflict at write time.

    The reference travels in ``planned_tpsl_json`` and is not a stop the gate
    has to judge: there is no explicit-price target left, so the recheck
    returns without reading a ticker or opening a session.
    """

    from telegram_kol_research import management_stop_price_gate as gate
    from telegram_kol_research.strategy_management_contracts import (
        ManagementInstructionContract,
        management_contract_fingerprint,
        serialize_management_contract,
    )

    contract = ManagementInstructionContract(
        version=2,
        target_lifecycle_id=1,
        strategy_instance_id="deepcoin:1:1:BTC:short",
        symbol="BTC",
        side="short",
        close_fraction="0.5",
        stop_mode="actual_entry_price",
        stop_price=None,
        stop_price_source=None,
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=True,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text="减仓移动止损到成本",
    )
    batch = _Batch(
        contract_json=serialize_management_contract(contract),
        legs=[
            _Leg(
                {
                    "intent": "partial_then_break_even",
                    "stop_loss_text": None,
                    "break_even_reference_price": "80500",
                    "break_even_reference_source": "strategy_first_leg",
                }
            )
        ],
    )

    def refuse_any_session():
        raise AssertionError("a neutralised batch must need no identity read")

    assert (
        gate.validate_batch_stops(
            refuse_any_session, batch=batch, client=None, now=NOW
        )
        is None
    )
    # The fingerprint the batch would carry names exactly this contract.
    assert len(management_contract_fingerprint(contract)) == 64


def test_percentage_configuration_and_boundary():
    assert check(stop_price="72000").reason_code is None
    assert check(stop_price="71999").reason_code == "management_stop_deviation_exceeded"
    assert (
        check(
            settings=trading_settings_from_payload(
                {"max_management_stop_deviation_pct": 1}
            )
        ).reason_code
        == "management_stop_deviation_exceeded"
    )


@pytest.mark.parametrize(
    "quote",
    [
        None,
        {},
        {"instrument_id": "ETH-USDT-SWAP", "price": "80000"},
        {
            "instrument_id": "BTC-USDT-SWAP",
            "price": "80000",
            "price_field": "last",
            "observed_at": (NOW - timedelta(seconds=31)).isoformat(),
        },
        {
            "instrument_id": "BTC-USDT-SWAP",
            "price": "80000",
            "price_field": "last",
            "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
        },
    ],
)
def test_missing_wrong_stale_or_future_market_never_falls_back_to_entry(quote):
    assert check(quote=quote).reason_code == "management_stop_reference_unavailable"


def test_provenance_remains_necessary():
    assert (
        check(stop_price_source="context").reason_code
        == "management_stop_provenance_invalid"
    )
    assert (
        check(current_message_text="").reason_code
        == "management_stop_provenance_invalid"
    )


def test_quote_is_checked_after_read_not_against_reused_dispatch_time(monkeypatch):
    from telegram_kol_research import management_stop_price_gate as gate

    checks = iter([NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)])
    monkeypatch.setattr(gate, "_stop_check_now", lambda: next(checks))
    planning_clock = gate.stop_gate_clock(NOW)
    quote = {
        "instrument_id": "BTC-USDT-SWAP",
        "price": "80000",
        "price_field": "last",
        "observed_at": (NOW + timedelta(seconds=1)).isoformat(),
    }
    assert check(quote=quote, now=planning_clock()).reason_code is None
    # Dispatcher reuses processed_at after planning; execution still checks now.
    execution_clock = gate.stop_gate_clock(NOW)
    quote["observed_at"] = (NOW + timedelta(seconds=2)).isoformat()
    assert check(quote=quote, now=execution_clock()).reason_code is None


def test_gate_settings_persist_without_schema_change(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.trading_settings import (
        load_trading_settings,
        save_trading_settings,
    )

    factory = create_session_factory(tmp_path / "settings.db")
    save_trading_settings(
        factory,
        {
            "max_management_stop_deviation_pct": 2.5,
            "management_stop_quote_max_age_seconds": 5,
        },
    )
    settings = load_trading_settings(factory)
    assert settings.max_management_stop_deviation_pct == 2.5
    assert settings.management_stop_quote_max_age_seconds == 5
    assert (
        check(settings=settings, stop_price="77000").reason_code
        == "management_stop_deviation_exceeded"
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nonfinite_configuration_fails_closed(bad):
    settings = trading_settings_from_payload({"max_management_stop_deviation_pct": bad})
    assert (
        check(settings=settings).reason_code == "management_stop_configuration_invalid"
    )
