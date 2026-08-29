import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.auto_trade_execution import execute_strategy_revision
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
    StrategyRevisionBatch,
    TradingSetting,
)
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.strategy_revision_planner import (
    advance_strategy_revision,
    plan_strategy_revision,
)
from telegram_kol_research.strategy_threads import (
    create_strategy_thread_for_lifecycle,
)


NOW = datetime(2026, 7, 27, 9, tzinfo=UTC)


def _seed_entry_exchange_authority(session_factory) -> None:
    result = seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=NOW,
    )
    assert result.seeded is True


def _authority_document(session_factory):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )

    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        return json.loads(row.value_json)


def _persist_revision_target(
    session_factory,
    *,
    leg_states=("submitted", "submitted"),
    pos_ids=(None, None),
    leg_sizes=(1, 1),
):
    with session_factory() as session:
        root = RawMessage(chat_id=101, message_id=1460, text="BTC 多单")
        revision = RawMessage(chat_id=101, message_id=1462, text="更新 BTC 多单")
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:101:1460:BTC:long",
            kol_id="group:101",
            chat_id=101,
            message_id=1460,
            symbol="BTC",
            side="long",
            status="open",
        )
        session.add_all([root, revision, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=101,
            message_id=1460,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        for index, (status, pos_id, size) in enumerate(
            zip(leg_states, pos_ids, leg_sizes, strict=True)
        ):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="limit",
                    order_id=f"ord-{index}",
                    client_order_id=f"client-{index}",
                    pos_id=pos_id,
                    attribution_status=("verified" if pos_id else "unassigned"),
                    status=status,
                    request_json=f'{{"sz":"{size}"}}',
                )
            )
        session.commit()
        lifecycle_id = lifecycle.id
        revision_id = revision.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory,
        lifecycle_id=lifecycle_id,
    )
    return revision_id, lifecycle_id, thread.id


def test_revision_cancels_every_pending_leg_before_replacement(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={
            "entry": "65100-65400",
            "stop_loss": "64500",
            "take_profit": "66000",
        },
        planned_at=NOW,
    )
    events = []

    def cancel_writer(**kwargs):
        events.append(("cancel", kwargs["order_id"]))
        return {"status": "confirmed_cancelled"}

    def replacement_writer(**kwargs):
        events.append(("replace", kwargs["remaining_fraction"]))
        return {"status": "confirmed"}

    result = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        cancel_leg_writer=cancel_writer,
        replacement_writer=replacement_writer,
        advanced_at=NOW,
    )

    assert result.status == "succeeded"
    assert events == [
        ("cancel", "ord-0"),
        ("cancel", "ord-1"),
        ("replace", 1.0),
    ]


def test_revision_unknown_cancel_never_submits_or_retries_replacement(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        planned_at=NOW,
    )
    calls = []

    def cancel_writer(**kwargs):
        calls.append(kwargs["order_id"])
        return {"status": "submit_unknown"}

    first = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        cancel_leg_writer=cancel_writer,
        replacement_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("replacement must not submit")
        ),
        advanced_at=NOW,
    )
    second = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        cancel_leg_writer=cancel_writer,
        replacement_writer=lambda **kwargs: None,
        advanced_at=NOW,
    )

    assert first.status == "recovery_required"
    assert second.status == "recovery_required"
    assert calls == ["ord-0"]


def test_leg_filled_during_revision_is_retained_and_reduces_replacement(tmp_path):
    session_factory = create_session_factory(tmp_path / "filled.db")
    raw_id, lifecycle_id, thread_id = _persist_revision_target(session_factory)
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        planned_at=NOW,
    )
    cancelled = []
    replacements = []

    result = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        read_leg_state=lambda **kwargs: (
            {"status": "filled", "pos_id": "pos-live"}
            if kwargs["order_id"] == "ord-0"
            else {"status": "pending"}
        ),
        cancel_leg_writer=lambda **kwargs: (
            cancelled.append(kwargs["order_id"])
            or {"status": "confirmed_cancelled"}
        ),
        replacement_writer=lambda **kwargs: (
            replacements.append(kwargs["remaining_fraction"])
            or {"status": "confirmed"}
        ),
        advanced_at=NOW,
    )

    assert result.status == "succeeded"
    assert cancelled == ["ord-1"]
    assert replacements == [0.5]
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        batch = session.get(StrategyRevisionBatch, plan.batch_id)
    assert lifecycle.lifecycle_status == "entered"
    assert batch.strategy_thread_id == thread_id


def test_explicit_new_order_and_non_unique_risk_increase_do_not_revise(tmp_path):
    session_factory = create_session_factory(tmp_path / "guard.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)

    explicit_new = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100"},
        explicit_new_thread=True,
        planned_at=NOW,
    )
    unsafe = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=None,
        replacement={"entry": "64000", "risk_change": "increase"},
        planned_at=NOW,
    )

    assert explicit_new.status == "new_thread_required"
    assert explicit_new.batch_id is None
    assert unsafe.status == "blocked"
    assert unsafe.reason_code == "revision_target_not_unique"


def test_auto_execution_refuses_to_cancel_without_replacement_writer(tmp_path):
    session_factory = create_session_factory(tmp_path / "writer-guard.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)

    result = execute_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        deepcoin_client=object(),
        replacement_writer=None,
        processed_at=NOW,
    )

    assert result == {
        "status": "blocked",
        "reason": "revision_replacement_writer_unavailable",
    }
    with session_factory() as session:
        assert session.query(StrategyRevisionBatch).count() == 0


def test_legacy_revision_respects_frozen_auto_trade_before_planning(tmp_path):
    session_factory = create_session_factory(tmp_path / "legacy-frozen.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": False},
        updated_at=NOW,
    )
    calls = []

    result = execute_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        deepcoin_client=object(),
        replacement_writer=lambda **kwargs: calls.append(kwargs),
        processed_at=NOW,
    )

    assert result == {"status": "blocked", "reason": "auto_trade_disabled"}
    assert calls == []
    with session_factory() as session:
        assert session.query(StrategyRevisionBatch).count() == 0


def test_cancellation_authority_blocks_legacy_revision_before_planning(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        acquire_entry_revision_exchange_authority,
    )

    session_factory = create_session_factory(tmp_path / "legacy-busy.db")
    _seed_entry_exchange_authority(session_factory)
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": False,
            "entry_revision_v2_mode": "disabled",
        },
        updated_at=NOW,
    )
    cancellation = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="reviewed_pending_entry_cancel",
        owner_id="order:reviewed-1",
        acquired_at=NOW,
        require_cancel_quiescence=True,
    )
    assert cancellation.acquired is True
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True},
        updated_at=NOW + timedelta(seconds=1),
    )

    result = execute_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        deepcoin_client=object(),
        replacement_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("replacement must not submit")
        ),
        processed_at=NOW + timedelta(seconds=2),
    )

    assert result == {
        "status": "in_progress",
        "reason": "entry_revision_exchange_authority_busy",
    }
    with session_factory() as session:
        assert session.query(StrategyRevisionBatch).count() == 0


def test_legacy_revision_unknown_cancel_retains_worker_authority(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.auto_trade_execution as auto_module

    session_factory = create_session_factory(tmp_path / "legacy-unknown.db")
    _seed_entry_exchange_authority(session_factory)
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True},
        updated_at=NOW,
    )
    monkeypatch.setattr(
        auto_module,
        "cancel_revision_entry_leg",
        lambda *_args, **_kwargs: {"status": "submit_unknown"},
    )

    result = execute_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        deepcoin_client=object(),
        replacement_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("replacement must not submit")
        ),
        processed_at=NOW,
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "revision_cancel_outcome_unknown"
    authority = _authority_document(session_factory)
    assert authority["state"] == "held"
    assert authority["owner_kind"] == "entry_revision_worker"


def test_legacy_revision_unknown_replacement_retains_worker_authority(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.auto_trade_execution as auto_module

    session_factory = create_session_factory(tmp_path / "legacy-replace-unknown.db")
    _seed_entry_exchange_authority(session_factory)
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True},
        updated_at=NOW,
    )
    monkeypatch.setattr(
        auto_module,
        "cancel_revision_entry_leg",
        lambda *_args, **_kwargs: {"status": "confirmed_cancelled"},
    )

    result = execute_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        deepcoin_client=object(),
        replacement_writer=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("replacement outcome unknown")
        ),
        processed_at=NOW,
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "revision_replacement_submit_unknown"
    authority = _authority_document(session_factory)
    assert authority["state"] == "held"
    assert authority["owner_kind"] == "entry_revision_worker"


def test_legacy_revision_post_cancel_size_drift_retains_worker_authority(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.auto_trade_execution as auto_module

    session_factory = create_session_factory(tmp_path / "legacy-size-drift.db")
    _seed_entry_exchange_authority(session_factory)
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True},
        updated_at=NOW,
    )
    cancel_calls = 0

    def cancel_then_corrupt_size(*_args, **_kwargs):
        nonlocal cancel_calls
        cancel_calls += 1
        if cancel_calls == 1:
            with session_factory() as session:
                for leg in session.query(ExecutionOrderLeg).all():
                    leg.request_json = '{"sz":"invalid"}'
                session.commit()
        return {"status": "confirmed_cancelled"}

    monkeypatch.setattr(
        auto_module,
        "cancel_revision_entry_leg",
        cancel_then_corrupt_size,
    )

    result = execute_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        deepcoin_client=object(),
        replacement_writer=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("replacement must not submit")
        ),
        processed_at=NOW,
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "revision_entry_leg_size_invalid"
    assert cancel_calls == 2
    authority = _authority_document(session_factory)
    assert authority["state"] == "held"
    assert authority["owner_kind"] == "entry_revision_worker"


def test_legacy_revision_success_owns_then_releases_worker_authority(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.auto_trade_execution as auto_module

    session_factory = create_session_factory(tmp_path / "legacy-success.db")
    _seed_entry_exchange_authority(session_factory)
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True},
        updated_at=NOW,
    )

    def cancel_under_authority(*_args, **_kwargs):
        authority = _authority_document(session_factory)
        assert authority["state"] == "held"
        assert authority["owner_kind"] == "entry_revision_worker"
        assert authority["action_id"] == f"legacy-raw:{raw_id}"
        assert "owner_id" not in authority
        return {"status": "confirmed_cancelled"}

    monkeypatch.setattr(
        auto_module,
        "cancel_revision_entry_leg",
        cancel_under_authority,
    )

    result = execute_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        deepcoin_client=object(),
        replacement_writer=lambda **_kwargs: {"status": "confirmed"},
        processed_at=NOW,
    )

    assert result["status"] == "succeeded"
    assert _authority_document(session_factory)["state"] == "idle"


def test_revision_remaining_exposure_uses_leg_size_not_leg_count(tmp_path):
    session_factory = create_session_factory(tmp_path / "weighted.db")
    raw_id, _, thread_id = _persist_revision_target(
        session_factory,
        leg_sizes=(1, 3),
    )
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        planned_at=NOW,
    )
    replacements = []

    result = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        read_leg_state=lambda **kwargs: (
            {"status": "filled", "pos_id": "pos-small"}
            if kwargs["order_id"] == "ord-0"
            else {"status": "pending"}
        ),
        cancel_leg_writer=lambda **kwargs: {"status": "confirmed_cancelled"},
        replacement_writer=lambda **kwargs: (
            replacements.append(kwargs["remaining_fraction"])
            or {"status": "confirmed"}
        ),
        advanced_at=NOW,
    )

    assert result.status == "succeeded"
    assert replacements == [0.75]


@pytest.mark.parametrize("interrupted_status", ["submitting_replacements", "reconciling"])
def test_revision_restart_after_replacement_boundary_never_resubmits(
    tmp_path,
    interrupted_status,
):
    session_factory = create_session_factory(tmp_path / f"{interrupted_status}.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        planned_at=NOW,
    )
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, plan.batch_id)
        batch.status = interrupted_status
        session.commit()

    result = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        cancel_leg_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("cancel must not resume")
        ),
        replacement_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("replacement must not resubmit")
        ),
        advanced_at=NOW,
    )

    assert result.status == interrupted_status
    assert result.reason_code == "revision_replacement_reconciliation_required"


def test_revision_concurrent_advance_claim_never_enters_writer(tmp_path):
    session_factory = create_session_factory(tmp_path / "concurrent-claim.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        planned_at=NOW,
    )
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, plan.batch_id)
        batch.advance_claim_token = "other-worker"
        batch.advance_claimed_at = NOW
        session.commit()

    result = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        cancel_leg_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("concurrent worker must not cancel")
        ),
        replacement_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("concurrent worker must not submit")
        ),
        advanced_at=NOW,
    )

    assert result.status == "in_progress"
    assert result.reason_code == "revision_advance_already_claimed"


def test_stale_revision_claim_freezes_for_reconciliation_without_writes(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-claim.db")
    raw_id, _, thread_id = _persist_revision_target(session_factory)
    plan = plan_strategy_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        replacement={"entry": "65100-65400"},
        planned_at=NOW,
    )
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, plan.batch_id)
        batch.status = "cancelling_old_entries"
        batch.advance_claim_token = "crashed-worker"
        batch.advance_claimed_at = NOW - timedelta(minutes=6)
        session.commit()

    result = advance_strategy_revision(
        session_factory,
        batch_id=plan.batch_id,
        cancel_leg_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("stale claim must not resume cancellation")
        ),
        replacement_writer=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("stale claim must not submit")
        ),
        advanced_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "revision_advance_claim_stale"
