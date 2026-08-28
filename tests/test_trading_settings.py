from decimal import Decimal
from datetime import UTC, datetime, timedelta
import json
import threading

import pytest

import telegram_kol_research.trading_settings as trading_settings_module
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.group_config import TrackedSenderConfig
from telegram_kol_research.trading_settings import SymbolEntryThresholds
from telegram_kol_research.trading_settings import apply_trading_settings_to_group_config
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.trading_settings import load_trading_settings
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.trading_settings import TradingSettings
from telegram_kol_research.trading_settings import trading_settings_from_payload
from telegram_kol_research.trading_settings import ENTRY_REVISION_ACTIVATION_KEY
from telegram_kol_research.models import TradingSetting


def test_load_trading_settings_returns_safe_defaults(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = load_trading_settings(session_factory)

    assert settings.auto_trade_enabled is False
    assert settings.default_max_loss_usdt == 20.0
    assert settings.max_concurrent_positions == 4
    assert settings.allowed_symbols == ["BTC", "ETH"]
    assert settings.entry_thresholds_for_symbol("BTC") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("200"),
        first_limit_offset=Decimal("90"),
        second_limit_offset=Decimal("90"),
    )
    assert settings.entry_thresholds_for_symbol("ETH") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("4"),
        first_limit_offset=Decimal("2"),
        second_limit_offset=Decimal("2"),
    )
    assert settings.entry_range_order_style == "eager"
    assert settings.nearby_entry_market_deviation_pct == 0.15
    assert settings.revision_target_min_confidence == 0.70
    assert settings.multi_instruction_mode == "disabled"
    assert settings.multi_instruction_activation_after_raw_message_id == 0
    assert settings.take_profit_allocations == [40.0, 30.0, 30.0]
    assert settings.allow_vision_auto_trade is True
    assert settings.context_resolution_enabled is False
    assert settings.context_resolution_live_chat_ids == []
    assert settings.context_resolution_enabled_for_chat(100) is False
    assert settings.entry_preamble_mode == "disabled"
    assert settings.entry_message_assembly_v2_mode == "disabled"
    assert settings.entry_revision_v2_mode == "disabled"
    assert settings.instruction_execution_contract_mode == "disabled"
    assert settings.instruction_execution_entry_after_item_id == 0
    assert settings.instruction_execution_management_after_item_id == 0
    assert settings.deepcoin_contract_specs_mode == "static"
    assert settings.mimo_contract_mode == "v1"
    assert settings.mimo_v2_activation_after_raw_message_id == 0
    assert settings.message_lock_mode == "global"
    assert settings.worker_command_mode == "inline"
    assert settings.semantic_review_enabled is False
    assert not hasattr(settings, "entry_preamble_live_chat_ids")


@pytest.mark.parametrize("mode", ["inline", "shadow", "queue"])
def test_worker_command_mode_round_trips_without_changing_message_modes(
    tmp_path, mode
):
    session_factory = create_session_factory(tmp_path / "worker-command-settings.db")
    save_trading_settings(
        session_factory,
        {"message_lock_mode": "global", "message_pipeline_mode": "queue"},
    )

    saved = save_trading_settings(session_factory, {"worker_command_mode": mode})
    reloaded = load_trading_settings(session_factory)

    assert saved.worker_command_mode == mode
    assert reloaded.worker_command_mode == mode
    assert reloaded.message_lock_mode == "global"
    assert reloaded.message_pipeline_mode == "queue"


@pytest.mark.parametrize("value", ["unsafe", "", True, False, [], {}, 1, None])
def test_worker_command_mode_rejects_values_that_could_enable_unknown_authority(
    value,
):
    with pytest.raises(ValueError, match="worker_command_mode"):
        trading_settings_from_payload({"worker_command_mode": value})


def test_semantic_review_enabled_round_trips_without_changing_runtime_modes(tmp_path):
    session_factory = create_session_factory(tmp_path / "semantic-review-settings.db")
    save_trading_settings(
        session_factory,
        {
            "message_lock_mode": "global",
            "message_pipeline_mode": "queue",
            "worker_command_mode": "shadow",
        },
    )

    enabled = save_trading_settings(
        session_factory, {"semantic_review_enabled": True}
    )
    reloaded = load_trading_settings(session_factory)

    assert enabled.semantic_review_enabled is True
    assert reloaded.semantic_review_enabled is True
    assert reloaded.message_lock_mode == "global"
    assert reloaded.message_pipeline_mode == "queue"
    assert reloaded.worker_command_mode == "shadow"


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_semantic_review_enabled_rejects_non_boolean_values(value):
    with pytest.raises(ValueError, match="semantic_review_enabled"):
        trading_settings_from_payload({"semantic_review_enabled": value})


def test_revision_target_min_confidence_round_trips_independently(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "min_ai_confidence": 0.81,
            "revision_target_min_confidence": 0.71,
        },
    )

    assert saved.min_ai_confidence == 0.81
    assert saved.revision_target_min_confidence == 0.71
    assert load_trading_settings(session_factory).revision_target_min_confidence == 0.71


@pytest.mark.parametrize("mode", ["disabled", "shadow", "live"])
def test_instruction_execution_rollout_settings_round_trip_without_loss(
    tmp_path, mode
):
    session_factory = create_session_factory(tmp_path / "execution-settings.db")
    save_trading_settings(
        session_factory,
        {
            "default_max_loss_usdt": 37,
            "allowed_symbols": ["BTC", "SOL"],
        },
    )

    saved = save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": mode,
            "instruction_execution_entry_after_item_id": 101,
            "instruction_execution_management_after_item_id": 202,
        },
    )

    assert saved.instruction_execution_contract_mode == mode
    assert saved.instruction_execution_entry_after_item_id == 101
    assert saved.instruction_execution_management_after_item_id == 202
    assert saved.default_max_loss_usdt == 37
    assert saved.allowed_symbols == ["BTC", "SOL"]


@pytest.mark.parametrize("value", ["unsafe", True, [], {}, 1, None])
def test_instruction_execution_mode_fails_closed(value):
    with pytest.raises(ValueError, match="instruction_execution_contract_mode"):
        trading_settings_from_payload(
            {"instruction_execution_contract_mode": value}
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "instruction_execution_entry_after_item_id",
        "instruction_execution_management_after_item_id",
    ],
)
@pytest.mark.parametrize("value", [-1, True, "42", 1.5, None])
def test_instruction_execution_watermarks_fail_closed(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        trading_settings_from_payload({field_name: value})


@pytest.mark.parametrize("mode", ["disabled", "shadow", "live"])
def test_multi_instruction_rollout_settings_round_trip(tmp_path, mode):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "multi_instruction_mode": mode,
            "multi_instruction_activation_after_raw_message_id": 42,
        },
    )

    assert saved.multi_instruction_mode == mode
    assert saved.multi_instruction_activation_after_raw_message_id == 42


@pytest.mark.parametrize("value", [-1, True, "42", 1.5, None])
def test_multi_instruction_watermark_fails_closed(value):
    with pytest.raises(ValueError, match="multi_instruction_activation"):
        trading_settings_from_payload(
            {"multi_instruction_activation_after_raw_message_id": value}
        )


@pytest.mark.parametrize("mode", ["v1", "v2_live_adapter"])
def test_mimo_contract_rollout_settings_round_trip(tmp_path, mode):
    session_factory = create_session_factory(tmp_path / "mimo-settings.db")

    saved = save_trading_settings(
        session_factory,
        {
            "mimo_contract_mode": mode,
            "mimo_v2_activation_after_raw_message_id": 42,
        },
    )

    assert saved.mimo_contract_mode == mode
    assert saved.mimo_v2_activation_after_raw_message_id == 42
    loaded = load_trading_settings(session_factory)
    assert loaded.mimo_contract_mode == mode
    assert loaded.mimo_v2_activation_after_raw_message_id == 42


@pytest.mark.parametrize("value", ["shadow", "v2", "live", True, [], {}, 1, None])
def test_mimo_contract_mode_fails_closed(value):
    with pytest.raises(ValueError, match="mimo_contract_mode"):
        trading_settings_from_payload({"mimo_contract_mode": value})


@pytest.mark.parametrize("mode", ["global", "per_chat"])
def test_message_lock_mode_round_trips_and_defaults_to_global(tmp_path, mode):
    session_factory = create_session_factory(tmp_path / "message-lock-settings.db")

    assert load_trading_settings(session_factory).message_lock_mode == "global"

    saved = save_trading_settings(session_factory, {"message_lock_mode": mode})

    assert saved.message_lock_mode == mode
    assert load_trading_settings(session_factory).message_lock_mode == mode


@pytest.mark.parametrize("value", ["shadow", "per-chat", True, [], {}, 1, None])
def test_message_lock_mode_fails_closed(value):
    with pytest.raises(ValueError, match="message_lock_mode"):
        trading_settings_from_payload({"message_lock_mode": value})


def test_message_parallel_chat_limit_defaults_to_compatibility_twenty(tmp_path):
    session_factory = create_session_factory(tmp_path / "parallel-chat-default.db")

    settings = load_trading_settings(session_factory)

    assert settings.message_processing_max_parallel_chats == 20


def test_message_parallel_chat_limit_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "parallel-chat-roundtrip.db")

    saved = save_trading_settings(
        session_factory,
        {"message_processing_max_parallel_chats": 3},
    )

    assert saved.message_processing_max_parallel_chats == 3
    assert (
        load_trading_settings(session_factory).message_processing_max_parallel_chats
        == 3
    )


@pytest.mark.parametrize(
    "value", [True, False, 0, -1, 21, 1.0, "3", None, [], {}]
)
def test_message_parallel_chat_limit_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="message_processing_max_parallel_chats"):
        trading_settings_from_payload(
            {"message_processing_max_parallel_chats": value}
        )


def test_unrelated_save_preserves_message_parallel_chat_limit_and_lock_mode(tmp_path):
    session_factory = create_session_factory(tmp_path / "parallel-chat-preserve.db")
    save_trading_settings(
        session_factory,
        {
            "message_lock_mode": "per_chat",
            "message_processing_max_parallel_chats": 3,
        },
    )

    saved = save_trading_settings(
        session_factory,
        {"semantic_review_enabled": False},
    )

    assert saved.message_lock_mode == "per_chat"
    assert saved.message_processing_max_parallel_chats == 3
    reloaded = load_trading_settings(session_factory)
    assert reloaded.message_lock_mode == "per_chat"
    assert reloaded.message_processing_max_parallel_chats == 3


def test_unrelated_settings_save_cannot_restore_stale_concurrency_tuple(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "stale-unrelated-save.db"
    unrelated_factory = create_session_factory(database_path)
    transition_factory = create_session_factory(database_path)
    save_trading_settings(
        unrelated_factory,
        {
            "message_lock_mode": "global",
            "message_processing_max_parallel_chats": 20,
            "default_max_loss_usdt": 20,
        },
    )
    stale_read = threading.Event()
    release_unrelated = threading.Event()
    transition_started = threading.Event()
    transition_finished = threading.Event()
    errors: list[BaseException] = []
    real_read = trading_settings_module._settings_row_and_payload_in_session

    def gated_read(session):
        result = real_read(session)
        if threading.current_thread().name == "unrelated-settings-writer":
            stale_read.set()
            if not release_unrelated.wait(timeout=3.0):
                raise AssertionError("unrelated settings writer was not released")
        return result

    monkeypatch.setattr(
        trading_settings_module,
        "_settings_row_and_payload_in_session",
        gated_read,
    )

    def unrelated_writer():
        try:
            save_trading_settings(
                unrelated_factory,
                {"default_max_loss_usdt": 25},
            )
        except BaseException as exc:
            errors.append(exc)

    def concurrency_writer():
        transition_started.set()
        try:
            _transition_concurrency(
                transition_factory,
                {
                    "message_lock_expected_mode": "global",
                    "message_processing_expected_max_parallel_chats": 20,
                    "message_lock_mode": "per_chat",
                    "message_processing_max_parallel_chats": 3,
                },
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            transition_finished.set()

    unrelated_thread = threading.Thread(
        target=unrelated_writer,
        name="unrelated-settings-writer",
    )
    transition_thread = threading.Thread(
        target=concurrency_writer,
        name="concurrency-settings-writer",
    )
    unrelated_thread.start()
    try:
        assert stale_read.wait(timeout=2.0)
        transition_thread.start()
        assert transition_started.wait(timeout=2.0)
        # On the broken path, the transition can commit while the unrelated
        # writer still holds a stale read. On the fixed path it waits for that
        # writer's short transaction to commit first.
        transition_finished.wait(timeout=0.25)
    finally:
        release_unrelated.set()
        unrelated_thread.join(timeout=3.0)
        if transition_thread.ident is not None:
            transition_thread.join(timeout=3.0)

    assert not unrelated_thread.is_alive()
    assert not transition_thread.is_alive()
    assert errors == []
    final = load_trading_settings(transition_factory)
    assert final.default_max_loss_usdt == 25
    assert (final.message_lock_mode, final.message_processing_max_parallel_chats) == (
        "per_chat",
        3,
    )


def _transition_concurrency(session_factory, payload):
    return trading_settings_module.transition_message_concurrency_settings(
        session_factory,
        payload,
        updated_at=datetime(2026, 8, 24, 0, 0, tzinfo=UTC),
    )


def test_concurrency_transition_writes_mode_and_cap_in_one_transaction(tmp_path):
    database_path = tmp_path / "atomic-concurrency.db"
    writer_factory = create_session_factory(database_path)
    reader_factory = create_session_factory(database_path)
    save_trading_settings(
        writer_factory,
        {
            "message_lock_mode": "global",
            "message_processing_max_parallel_chats": 20,
        },
    )
    observed: list[tuple[str, int]] = []
    ready = threading.Event()
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            settings = load_trading_settings(reader_factory)
            observed.append(
                (
                    settings.message_lock_mode,
                    settings.message_processing_max_parallel_chats,
                )
            )
            ready.set()

    thread = threading.Thread(target=reader)
    thread.start()
    assert ready.wait(timeout=2.0)
    try:
        expected_mode = "global"
        expected_cap = 20
        for _ in range(10):
            target_mode = "per_chat" if expected_mode == "global" else "global"
            target_cap = 3 if expected_cap == 20 else 20
            _transition_concurrency(
                writer_factory,
                {
                    "message_lock_expected_mode": expected_mode,
                    "message_processing_expected_max_parallel_chats": expected_cap,
                    "message_lock_mode": target_mode,
                    "message_processing_max_parallel_chats": target_cap,
                },
            )
            expected_mode = target_mode
            expected_cap = target_cap
    finally:
        stop.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert observed
    assert set(observed) <= {("global", 20), ("per_chat", 3)}
    final = load_trading_settings(reader_factory)
    assert (
        final.message_lock_mode,
        final.message_processing_max_parallel_chats,
    ) == ("global", 20)


def test_concurrency_transition_rejects_expected_mode_mismatch_without_write(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "expected-mode.db")
    save_trading_settings(
        session_factory,
        {
            "message_lock_mode": "global",
            "message_processing_max_parallel_chats": 20,
        },
    )

    with pytest.raises(ValueError, match="expected message lock mode"):
        _transition_concurrency(
            session_factory,
            {
                "message_lock_expected_mode": "per_chat",
                "message_processing_expected_max_parallel_chats": 20,
                "message_lock_mode": "per_chat",
                "message_processing_max_parallel_chats": 3,
            },
        )

    settings = load_trading_settings(session_factory)
    assert (settings.message_lock_mode, settings.message_processing_max_parallel_chats) == (
        "global",
        20,
    )


def test_concurrency_transition_rejects_expected_cap_mismatch_without_write(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "expected-cap.db")
    save_trading_settings(
        session_factory,
        {
            "message_lock_mode": "global",
            "message_processing_max_parallel_chats": 20,
        },
    )

    with pytest.raises(ValueError, match="expected parallel chat limit"):
        _transition_concurrency(
            session_factory,
            {
                "message_lock_expected_mode": "global",
                "message_processing_expected_max_parallel_chats": 3,
                "message_lock_mode": "per_chat",
                "message_processing_max_parallel_chats": 3,
            },
        )

    settings = load_trading_settings(session_factory)
    assert (settings.message_lock_mode, settings.message_processing_max_parallel_chats) == (
        "global",
        20,
    )


def test_global_to_per_chat_requires_both_target_and_expected_fields(tmp_path):
    session_factory = create_session_factory(tmp_path / "required-transition.db")
    complete = {
        "message_lock_expected_mode": "global",
        "message_processing_expected_max_parallel_chats": 20,
        "message_lock_mode": "per_chat",
        "message_processing_max_parallel_chats": 3,
    }

    for missing in (
        "message_processing_max_parallel_chats",
        "message_lock_expected_mode",
        "message_processing_expected_max_parallel_chats",
    ):
        payload = dict(complete)
        payload.pop(missing)
        with pytest.raises(ValueError, match="global to per_chat"):
            _transition_concurrency(session_factory, payload)
        settings = load_trading_settings(session_factory)
        assert (
            settings.message_lock_mode,
            settings.message_processing_max_parallel_chats,
        ) == ("global", 20)


@pytest.mark.parametrize(
    ("expected_field", "expected_value", "target_field"),
    [
        ("message_lock_expected_mode", "global", "message_lock_mode"),
        (
            "message_processing_expected_max_parallel_chats",
            20,
            "message_processing_max_parallel_chats",
        ),
    ],
)
def test_expected_concurrency_field_requires_matching_target(
    tmp_path,
    expected_field,
    expected_value,
    target_field,
):
    session_factory = create_session_factory(tmp_path / f"unpaired-{target_field}.db")

    with pytest.raises(ValueError, match="requires the matching target field"):
        _transition_concurrency(
            session_factory,
            {expected_field: expected_value},
        )


def test_global_rollback_can_keep_cap_three(tmp_path):
    session_factory = create_session_factory(tmp_path / "rollback-keep-cap.db")
    save_trading_settings(
        session_factory,
        {
            "message_lock_mode": "per_chat",
            "message_processing_max_parallel_chats": 3,
        },
    )

    saved = _transition_concurrency(
        session_factory,
        {
            "message_lock_expected_mode": "per_chat",
            "message_lock_mode": "global",
        },
    )

    assert (saved.message_lock_mode, saved.message_processing_max_parallel_chats) == (
        "global",
        3,
    )


def test_fail_closed_rollback_can_set_global_and_cap_one_atomically(tmp_path):
    session_factory = create_session_factory(tmp_path / "rollback-cap-one.db")
    save_trading_settings(
        session_factory,
        {
            "message_lock_mode": "per_chat",
            "message_processing_max_parallel_chats": 3,
        },
    )

    saved = _transition_concurrency(
        session_factory,
        {
            "message_lock_expected_mode": "per_chat",
            "message_processing_expected_max_parallel_chats": 3,
            "message_lock_mode": "global",
            "message_processing_max_parallel_chats": 1,
        },
    )

    assert (saved.message_lock_mode, saved.message_processing_max_parallel_chats) == (
        "global",
        1,
    )


def test_authoritative_gap_recovery_max_age_minutes_defaults_and_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "gap-recovery-window.db")

    # Default is unchanged from the hardcoded 15-minute constant it replaces.
    assert (
        load_trading_settings(session_factory).authoritative_gap_recovery_max_age_minutes
        == 15.0
    )

    saved = save_trading_settings(
        session_factory, {"authoritative_gap_recovery_max_age_minutes": 45.0}
    )

    assert saved.authoritative_gap_recovery_max_age_minutes == 45.0
    assert (
        load_trading_settings(session_factory).authoritative_gap_recovery_max_age_minutes
        == 45.0
    )


@pytest.mark.parametrize("value", [0, -5, "not-a-number", None])
def test_authoritative_gap_recovery_max_age_minutes_fails_open_to_default(value):
    """Invalid values fail OPEN to the safe 15-minute default, not raise -
    matching every other ``_positive_float`` setting in this module, so a bad
    payload cannot brick settings load and silently disable recovery."""

    settings = trading_settings_from_payload(
        {"authoritative_gap_recovery_max_age_minutes": value}
    )
    assert settings.authoritative_gap_recovery_max_age_minutes == 15.0


@pytest.mark.parametrize("value", [-1, True, "42", 1.5, None])
def test_mimo_v2_activation_watermark_fails_closed(value):
    with pytest.raises(ValueError, match="mimo_v2_activation_after_raw_message_id"):
        trading_settings_from_payload(
            {"mimo_v2_activation_after_raw_message_id": value}
        )


@pytest.mark.parametrize("mode", ["static", "shadow", "live"])
def test_deepcoin_contract_specs_mode_round_trips(mode, tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {"deepcoin_contract_specs_mode": mode.upper()},
    )

    assert saved.deepcoin_contract_specs_mode == mode
    assert load_trading_settings(session_factory).deepcoin_contract_specs_mode == mode


@pytest.mark.parametrize("value", ["disabled", "unsafe", True, [], {}, 1, None])
def test_deepcoin_contract_specs_mode_fails_closed(value):
    with pytest.raises(ValueError, match="deepcoin_contract_specs_mode"):
        trading_settings_from_payload({"deepcoin_contract_specs_mode": value})


def test_contract_spec_cache_controls_are_not_web_trading_settings():
    settings = trading_settings_from_payload(
        {
            "deepcoin_contract_specs_cache_path": "/tmp/untrusted.json",
            "deepcoin_contract_specs_ttl_hours": 999,
        }
    )

    assert not hasattr(settings, "deepcoin_contract_specs_cache_path")
    assert not hasattr(settings, "deepcoin_contract_specs_ttl_hours")


def test_entry_revision_activation_generation_changes_only_with_mode(tmp_path):
    session_factory = create_session_factory(tmp_path / "revision-activation.db")
    activated_at = datetime(2026, 8, 8, 12, tzinfo=UTC)
    save_trading_settings(
        session_factory,
        {"entry_revision_v2_mode": "shadow"},
        updated_at=activated_at,
    )
    save_trading_settings(
        session_factory,
        {"default_max_loss_usdt": 30},
        updated_at=activated_at + timedelta(hours=1),
    )

    with session_factory() as session:
        activation = (
            session.query(TradingSetting)
            .filter_by(key=ENTRY_REVISION_ACTIVATION_KEY)
            .one()
        )
        assert json.loads(activation.value_json) == {"mode": "shadow"}
        assert activation.updated_at == activated_at.replace(tzinfo=None)


def test_entry_preamble_rollout_settings_ignore_legacy_allowlist(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "entry_preamble_mode": " LIVE ",
            "entry_preamble_live_chat_ids": [-1002, -1001, -1002],
        },
    )

    assert saved.entry_preamble_mode == "live"
    assert not hasattr(saved, "entry_preamble_live_chat_ids")
    assert load_trading_settings(session_factory).entry_preamble_mode == "live"


@pytest.mark.parametrize("value", ["unsafe", True, [], {}, 1, None])
def test_entry_preamble_mode_fails_closed(value):
    with pytest.raises(ValueError, match="entry_preamble_mode"):
        trading_settings_from_payload({"entry_preamble_mode": value})


@pytest.mark.parametrize(
    "field_name",
    ["entry_message_assembly_v2_mode", "entry_revision_v2_mode"],
)
def test_adjacent_entry_modes_round_trip_live(tmp_path, field_name):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(session_factory, {field_name: " LIVE "})

    assert getattr(saved, field_name) == "live"
    assert getattr(load_trading_settings(session_factory), field_name) == "live"


@pytest.mark.parametrize(
    "field_name",
    ["entry_message_assembly_v2_mode", "entry_revision_v2_mode"],
)
@pytest.mark.parametrize("value", ["unsafe", True, [], {}, 1, None])
def test_adjacent_entry_modes_fail_closed(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        trading_settings_from_payload({field_name: value})


@pytest.mark.parametrize("value", ["-1001", [0], [-1001, "-1002"], {}])
def test_entry_preamble_legacy_allowlist_is_ignored(value):
    settings = trading_settings_from_payload({"entry_preamble_live_chat_ids": value})

    assert not hasattr(settings, "entry_preamble_live_chat_ids")


def test_saving_settings_preserves_legacy_entry_preamble_allowlist_in_storage(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            TradingSetting(
                key="global",
                value_json=json.dumps(
                    {
                        "entry_preamble_mode": "disabled",
                        "entry_preamble_live_chat_ids": [-1002, -1001],
                    }
                ),
            )
        )
        session.commit()

    save_trading_settings(session_factory, {"auto_trade_enabled": False})

    with session_factory() as session:
        stored = json.loads(session.query(TradingSetting).filter_by(key="global").one().value_json)
    assert stored["entry_preamble_live_chat_ids"] == [-1002, -1001]


def test_saving_only_entry_preamble_mode_preserves_all_other_settings(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 77,
            "allowed_symbols": ["SOL"],
            "management_execution_mode": "live",
        },
    )

    updated = save_trading_settings(
        session_factory,
        {"entry_preamble_mode": "live"},
    )

    assert updated.entry_preamble_mode == "live"
    assert updated.auto_trade_enabled is True
    assert updated.default_max_loss_usdt == 77
    assert updated.allowed_symbols == ["SOL"]
    assert updated.management_execution_mode == "live"


def test_context_resolution_requires_live_trading_and_allowlisted_chat():
    settings = trading_settings_from_payload(
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "context_resolution_enabled": True,
            "context_resolution_live_chat_ids": [100, 200],
        }
    )

    assert settings.context_resolution_enabled_for_chat(100) is True
    assert settings.context_resolution_enabled_for_chat(300) is False

    disabled = trading_settings_from_payload(
        {
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
            "context_resolution_enabled": True,
            "context_resolution_live_chat_ids": [100],
        }
    )
    assert disabled.context_resolution_enabled_for_chat(100) is False


@pytest.mark.parametrize("value", ["true", 1, [], {}])
def test_context_resolution_enabled_is_strict_boolean(value):
    with pytest.raises(ValueError, match="context_resolution_enabled"):
        trading_settings_from_payload({"context_resolution_enabled": value})


@pytest.mark.parametrize("value", ["100", [0], [100, "200"], [100, 100], {}])
def test_context_resolution_chat_allowlist_fails_closed(value):
    with pytest.raises(ValueError, match="context_resolution_live_chat_ids"):
        trading_settings_from_payload({"context_resolution_live_chat_ids": value})


def test_management_execution_mode_defaults_disabled_and_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = load_trading_settings(session_factory)

    assert settings.management_execution_mode == "disabled"
    assert settings.management_planning_enabled is False
    assert settings.live_management_execution_enabled is False


def test_management_execution_mode_shadow_plans_without_global_auto_trade():
    settings = trading_settings_from_payload(
        {
            "management_execution_mode": "shadow",
            "auto_trade_enabled": False,
        }
    )

    assert settings.management_planning_enabled is True
    assert settings.live_management_execution_enabled is False


def test_management_execution_mode_live_requires_global_auto_trade():
    gated = trading_settings_from_payload(
        {
            "management_execution_mode": "live",
            "auto_trade_enabled": False,
        }
    )
    enabled = trading_settings_from_payload(
        {
            "management_execution_mode": "live",
            "auto_trade_enabled": True,
        }
    )

    assert gated.management_planning_enabled is False
    assert gated.live_management_execution_enabled is False
    assert enabled.management_planning_enabled is True
    assert enabled.live_management_execution_enabled is True


def test_auto_trade_disabled_plus_deployment_freeze_keeps_management_disabled(
    monkeypatch,
):
    monkeypatch.setenv("TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN", "1")
    settings = trading_settings_from_payload(
        {
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
            "composite_management_v2_mode": "live",
            "trigger_protection_stop_rescue_mode": "live",
            "position_management_liveness_v2_mode": "live",
        }
    )

    assert settings.entry_submission_enabled is False
    assert settings.management_planning_enabled is False
    assert settings.live_management_execution_enabled is False
    assert settings.effective_composite_management_v2_mode == "disabled"
    assert settings.effective_trigger_protection_stop_rescue_mode == "disabled"
    assert settings.effective_position_management_liveness_v2_mode == "disabled"


def test_entry_submission_requires_auto_trade():
    defaults = TradingSettings()
    enabled = trading_settings_from_payload({"auto_trade_enabled": True})

    assert defaults.entry_submission_enabled is False
    assert enabled.entry_submission_enabled is True


@pytest.mark.parametrize("value", [True, False, "false", 0, None])
def test_ordinary_settings_payload_rejects_legacy_entry_submission_frozen(value):
    with pytest.raises(ValueError, match="legacy_entry_submission_frozen"):
        trading_settings_from_payload(
            {"legacy_entry_submission_frozen": value}
        )


def test_management_execution_mode_rejects_invalid_value():
    with pytest.raises(ValueError, match="management_execution_mode"):
        trading_settings_from_payload({"management_execution_mode": "unsafe"})


@pytest.mark.parametrize("value", ["false", "0", 0, 1])
def test_auto_trade_enabled_rejects_non_boolean_values(value):
    with pytest.raises(ValueError, match="auto_trade_enabled"):
        trading_settings_from_payload(
            {
                "management_execution_mode": "live",
                "auto_trade_enabled": value,
            }
        )


@pytest.mark.parametrize("field", [
    "move_stop_to_breakeven_after_tp1",
    "allow_vision_auto_trade",
])
def test_trading_settings_boolean_fields_use_strict_validation(field):
    with pytest.raises(ValueError, match=field):
        trading_settings_from_payload({field: "false"})


@pytest.mark.parametrize("value", [[], {}, 1, None])
def test_management_execution_mode_rejects_non_string_values(value):
    with pytest.raises(ValueError, match="management_execution_mode"):
        trading_settings_from_payload({"management_execution_mode": value})


def test_management_execution_mode_normalizes_allowed_string():
    settings = trading_settings_from_payload(
        {"management_execution_mode": " LIVE ", "auto_trade_enabled": True}
    )

    assert settings.management_execution_mode == "live"
    assert settings.live_management_execution_enabled is True


def test_composite_management_v2_mode_defaults_disabled():
    settings = TradingSettings()

    assert settings.composite_management_v2_mode == "disabled"
    assert settings.effective_composite_management_v2_mode == "disabled"


def test_composite_management_v2_live_requires_both_existing_live_gates():
    global_off = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "live",
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
        }
    )
    management_off = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "shadow",
        }
    )
    enabled = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        }
    )

    assert global_off.effective_composite_management_v2_mode == "disabled"
    assert management_off.effective_composite_management_v2_mode == "disabled"
    assert enabled.effective_composite_management_v2_mode == "live"


def test_composite_management_v2_shadow_never_enables_exchange_writes():
    settings = trading_settings_from_payload(
        {
            "composite_management_v2_mode": "shadow",
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        }
    )

    assert settings.effective_composite_management_v2_mode == "shadow"


@pytest.mark.parametrize("value", [True, False, "unsafe", [], {}, 1, None])
def test_composite_management_v2_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="composite_management_v2_mode"):
        trading_settings_from_payload({"composite_management_v2_mode": value})


def test_trigger_protection_stop_rescue_defaults_disabled_and_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "rescue-settings.db")

    defaults = load_trading_settings(session_factory)
    saved = save_trading_settings(
        session_factory,
        {
            "trigger_protection_stop_rescue_mode": " SHADOW ",
        },
    )
    reloaded = load_trading_settings(session_factory)

    assert defaults.trigger_protection_stop_rescue_mode == "disabled"
    assert defaults.effective_trigger_protection_stop_rescue_mode == "disabled"
    assert saved.trigger_protection_stop_rescue_mode == "shadow"
    assert reloaded.trigger_protection_stop_rescue_mode == "shadow"
    assert reloaded.effective_trigger_protection_stop_rescue_mode == "shadow"


def test_position_management_liveness_v2_defaults_disabled_and_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "liveness-v2-settings.db")

    defaults = load_trading_settings(session_factory)
    saved = save_trading_settings(
        session_factory,
        {"position_management_liveness_v2_mode": " SHADOW "},
    )
    reloaded = load_trading_settings(session_factory)

    assert defaults.position_management_liveness_v2_mode == "disabled"
    assert defaults.effective_position_management_liveness_v2_mode == "disabled"
    assert saved.position_management_liveness_v2_mode == "shadow"
    assert reloaded.effective_position_management_liveness_v2_mode == "shadow"


def test_live_position_management_liveness_v2_requires_both_execution_gates():
    global_off = trading_settings_from_payload({
        "position_management_liveness_v2_mode": "live",
        "auto_trade_enabled": False,
        "management_execution_mode": "live",
    })
    management_off = trading_settings_from_payload({
        "position_management_liveness_v2_mode": "live",
        "auto_trade_enabled": True,
        "management_execution_mode": "shadow",
    })
    enabled = trading_settings_from_payload({
        "position_management_liveness_v2_mode": "live",
        "auto_trade_enabled": True,
        "management_execution_mode": "live",
    })

    assert global_off.effective_position_management_liveness_v2_mode == "disabled"
    assert management_off.effective_position_management_liveness_v2_mode == "disabled"
    assert enabled.effective_position_management_liveness_v2_mode == "live"


@pytest.mark.parametrize("value", [True, False, "unsafe", [], {}, 1, None])
def test_position_management_liveness_v2_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="position_management_liveness_v2_mode"):
        trading_settings_from_payload({"position_management_liveness_v2_mode": value})


def test_live_trigger_protection_stop_rescue_requires_both_execution_gates():
    disabled_globally = trading_settings_from_payload(
        {
            "trigger_protection_stop_rescue_mode": "live",
            "auto_trade_enabled": False,
            "management_execution_mode": "live",
        }
    )
    disabled_management = trading_settings_from_payload(
        {
            "trigger_protection_stop_rescue_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "shadow",
        }
    )
    enabled = trading_settings_from_payload(
        {
            "trigger_protection_stop_rescue_mode": "live",
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        }
    )

    assert disabled_globally.effective_trigger_protection_stop_rescue_mode == "disabled"
    assert disabled_management.effective_trigger_protection_stop_rescue_mode == "disabled"
    assert enabled.effective_trigger_protection_stop_rescue_mode == "live"


@pytest.mark.parametrize("value", ["unsafe", [], {}, 1, None])
def test_trigger_protection_stop_rescue_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="trigger_protection_stop_rescue_mode"):
        trading_settings_from_payload(
            {"trigger_protection_stop_rescue_mode": value}
        )


def test_save_trading_settings_normalizes_user_input(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": "120",
            "allowed_symbols": "btc, eth, sol",
            "symbol_max_loss_usdt": {
                "btc": "20",
                "ETH": "15.5",
                "bad": "0",
                "empty": "",
            },
            "symbol_entry_thresholds": {
                "btc": {
                    "market_leg_threshold": "200",
                    "first_limit_offset": "90",
                    "second_limit_offset": "90",
                },
                "doge": {
                    "market_leg_threshold": "0.01",
                    "first_limit_offset": "0.002",
                    "second_limit_offset": "0.003",
                },
            },
            "nearby_entry_market_deviation_pct": "1.2",
            "take_profit_allocations": "50,25,25",
            "entry_range_order_style": "eager",
        },
    )
    reloaded = load_trading_settings(session_factory)

    assert saved.auto_trade_enabled is True
    assert reloaded.default_max_loss_usdt == 120.0
    assert reloaded.allowed_symbols == ["BTC", "ETH", "SOL"]
    assert reloaded.symbol_max_loss_usdt == {"BTC": 20.0, "ETH": 15.5}
    assert reloaded.to_dict()["symbol_entry_thresholds"]["DOGE"] == {
        "market_leg_threshold": "0.01",
        "first_limit_offset": "0.002",
        "second_limit_offset": "0.003",
    }
    assert reloaded.max_loss_for_symbol("btc") == 20.0
    assert reloaded.max_loss_for_symbol("SOL") == 120.0
    assert reloaded.nearby_entry_market_deviation_pct == 1.2
    assert reloaded.take_profit_allocations == [50.0, 25.0, 25.0]
    assert reloaded.entry_range_order_style == "eager"
    assert reloaded.allow_vision_auto_trade is True


def test_legacy_settings_seed_initial_fixed_entry_thresholds(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = save_trading_settings(
        session_factory,
        {
            "allowed_symbols": ["BTC", "ETH", "SOL"],
            "symbol_max_loss_usdt": {"BTC": 20, "ETH": 15, "SOL": 10},
        },
    )

    assert settings.entry_thresholds_for_symbol("BTC") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("200"),
        first_limit_offset=Decimal("90"),
        second_limit_offset=Decimal("90"),
    )
    assert settings.entry_thresholds_for_symbol("ETH") == SymbolEntryThresholds(
        market_leg_threshold=Decimal("4"),
        first_limit_offset=Decimal("2"),
        second_limit_offset=Decimal("2"),
    )
    assert settings.entry_thresholds_for_symbol("SOL") == SymbolEntryThresholds.zero()


def test_legacy_save_preserves_persisted_fixed_entry_thresholds(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "250",
                    "first_limit_offset": "100",
                    "second_limit_offset": "95",
                },
                "DOGE": {
                    "market_leg_threshold": "0.01",
                    "first_limit_offset": "0.002",
                    "second_limit_offset": "0.003",
                },
            },
        },
    )

    saved = save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": False,
            "max_market_entry_deviation_pct": "0.2",
            "entry_range_order_style": "conservative",
        },
    )

    assert saved.auto_trade_enabled is False
    assert saved.symbol_entry_thresholds == {
        "BTC": {
            "market_leg_threshold": "250",
            "first_limit_offset": "100",
            "second_limit_offset": "95",
        },
        "DOGE": {
            "market_leg_threshold": "0.01",
            "first_limit_offset": "0.002",
            "second_limit_offset": "0.003",
        },
    }
    assert load_trading_settings(session_factory).symbol_entry_thresholds == (
        saved.symbol_entry_thresholds
    )


def test_symbol_entry_thresholds_preserve_small_decimals(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    settings = save_trading_settings(
        session_factory,
        {
            "symbol_entry_thresholds": {
                "PEPE": {
                    "market_leg_threshold": "0.000003",
                    "first_limit_offset": "0.000001",
                    "second_limit_offset": "0.000002",
                }
            }
        },
    )

    assert settings.to_dict()["symbol_entry_thresholds"]["PEPE"] == {
        "market_leg_threshold": "0.000003",
        "first_limit_offset": "0.000001",
        "second_limit_offset": "0.000002",
    }


@pytest.mark.parametrize("invalid", ["-1", -0.1, "nan", "inf", {}, []])
def test_symbol_entry_thresholds_reject_invalid_values(invalid):
    with pytest.raises(ValueError):
        trading_settings_from_payload(
            {
                "symbol_entry_thresholds": {
                    "BTC": {
                        "market_leg_threshold": invalid,
                        "first_limit_offset": "0",
                        "second_limit_offset": "0",
                    }
                }
            }
        )


def test_symbol_entry_thresholds_reject_magnitude_above_finite_float_max():
    with pytest.raises(ValueError, match="market_leg_threshold"):
        trading_settings_from_payload(
            {
                "symbol_entry_thresholds": {
                    "BTC": {
                        "market_leg_threshold": "1e1000",
                        "first_limit_offset": "0",
                        "second_limit_offset": "0",
                    }
                }
            }
        )


@pytest.mark.parametrize("value, expected", [
    ("40,20,20,20", [40.0, 20.0, 20.0, 20.0]),
    ("40,15,15,15,15", [40.0, 15.0, 15.0, 15.0, 15.0]),
])
def test_trading_settings_preserves_four_and_five_stage_allocations(tmp_path, value, expected):
    session_factory = create_session_factory(tmp_path / "research.db")

    saved = save_trading_settings(session_factory, {"take_profit_allocations": value})

    assert saved.take_profit_allocations == expected
    assert load_trading_settings(session_factory).take_profit_allocations == expected


@pytest.mark.parametrize("value", ["", "40,0,30", "20,20,20,20,20,20"])
def test_trading_settings_rejects_invalid_take_profit_allocation_shape(value):
    with pytest.raises(ValueError, match="take_profit_allocations"):
        trading_settings_from_payload({"take_profit_allocations": value})


def test_apply_trading_settings_to_group_config_preserves_sender_overrides():
    config = GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="vip",
                chat_id=100,
                trading_mode="auto_trade",
                max_loss_usdt=50,
                symbol_whitelist=["BTC"],
                tracked_senders=[
                    TrackedSenderConfig(
                        display_name="alice",
                        max_loss_usdt=25,
                    )
                ],
            )
        ]
    )

    runtime_config = apply_trading_settings_to_group_config(
        config,
        TradingSettings(
            default_max_loss_usdt=120,
            allowed_symbols=["SOL", "BTC"],
            symbol_max_loss_usdt={"SOL": 10.0},
        ),
    )

    assert runtime_config.groups[0].max_loss_usdt == 120.0
    assert runtime_config.groups[0].symbol_whitelist == ["SOL", "BTC"]
    assert runtime_config.groups[0].symbol_max_loss_usdt == {"SOL": 10.0}
    assert runtime_config.groups[0].tracked_senders[0].max_loss_usdt == 25
def test_source_deletion_exit_defaults_dormant_and_round_trips(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    defaults = load_trading_settings(session_factory)
    assert defaults.telegram_source_deletion_exit_enabled is False

    saved = save_trading_settings(
        session_factory,
        {"telegram_source_deletion_exit_enabled": True},
    )

    assert saved.telegram_source_deletion_exit_enabled is True
    assert (
        load_trading_settings(session_factory).telegram_source_deletion_exit_enabled
        is True
    )
