import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.entry_revision_planner import plan_entry_revision
from telegram_kol_research.models import (
    EntryRevisionReplacement,
    EntryStrategyFragment,
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageEvidenceVersion,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    TradingSetting,
)
from telegram_kol_research.strategy_threads import create_strategy_thread_for_lifecycle
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 8, 13, tzinfo=UTC)
_BASE_CREATE_SESSION_FACTORY = create_session_factory


def create_session_factory(*args, **kwargs):
    session_factory = _BASE_CREATE_SESSION_FACTORY(*args, **kwargs)
    result = seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=NOW,
    )
    if not result.seeded and result.reason_code != (
        "entry_revision_exchange_authority_already_exists"
    ):
        raise AssertionError(result.reason_code)
    return session_factory


def test_native_tpsl_without_pos_id_uses_frozen_ledger_order_identity():
    from telegram_kol_research.entry_revision_executor import _verified_stop_row

    result = _verified_stop_row(
        {
            "ordId": "owned-stop-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "triggerOrderType": "TPSL",
            "slTriggerPx": "65000",
            "sz": "0",
        },
        pos_id="pos-1",
        instrument_id="BTC-USDT-SWAP",
        position_side="short",
        required_size="0.01",
        owned_order_ids=frozenset({"owned-stop-1"}),
    )

    assert result["status"] == "verified"


def test_frozen_ledger_identity_cannot_override_contradictory_position_id():
    from telegram_kol_research.entry_revision_executor import _verified_stop_row

    result = _verified_stop_row(
        {
            "ordId": "owned-stop-1",
            "posId": "other-position",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "triggerOrderType": "TPSL",
            "slTriggerPx": "65000",
            "sz": "0",
        },
        pos_id="pos-1",
        instrument_id="BTC-USDT-SWAP",
        position_side="short",
        required_size="0.01",
        owned_order_ids=frozenset({"owned-stop-1"}),
    )

    assert result is None


def _planned_batch(session_factory, *, mode="live"):
    save_trading_settings(
        session_factory,
        {"entry_revision_v2_mode": mode},
        updated_at=NOW,
    )
    with session_factory() as session:
        strategy = RawMessage(chat_id=201, message_id=2001, text="BTC short")
        revision = RawMessage(chat_id=201, message_id=2002, text="add 63400")
        session.add_all([strategy, revision])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            parse_source="authoritative",
            confidence=1,
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:201:2001:BTC:short",
            kol_id="group:201",
            chat_id=201,
            message_id=2001,
            symbol="BTC",
            side="short",
            status="open",
        )
        session.add_all([candidate, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=201,
            message_id=2001,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        draft = {
            "strategy_instance_id": binding.strategy_instance_id,
            "instrument_id": "BTC-USDT-SWAP",
            "stop_loss": 65000,
            "risk_budget_usdt": 10,
            "contract_spec": {
                "instrument_id": "BTC-USDT-SWAP",
                "contract_value": 0.001,
                "quantity_step": 1,
                "min_quantity": 1,
                "price_tick": 0.1,
            },
            "order_legs": [
                {
                    "price": 64000,
                    "order_type": "limit",
                    "quantity": 0.005,
                    "client_order_id": "new-0",
                },
                {
                    "price": 63400,
                    "order_type": "limit",
                    "quantity": 0.003125,
                    "client_order_id": "new-1",
                },
            ],
        }
        assembly = EntryStrategyAssembly(
            strategy_raw_message_id=strategy.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=binding.strategy_instance_id,
            risk_multiplier="0.5",
            evidence_json=json.dumps(
                {
                    "configured_risk_budget_usdt": "20",
                    "effective_risk_budget_usdt": "10",
                    "order_draft_snapshot": draft,
                },
                sort_keys=True,
            ),
            fingerprint="b" * 64,
        )
        session.add(assembly)
        session.flush()
        for index in range(2):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id=f"old-{index}",
                    client_order_id=f"old-client-{index}",
                    attribution_status="verified",
                    last_verified_at=NOW,
                    status="submitted",
                    request_json=json.dumps(
                        {"sz": "0.005", "px": str(64000 - index * 200)}
                    ),
                )
            )
        session.commit()
        ids = revision.id, lifecycle.id, assembly.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory, lifecycle_id=ids[1]
    )
    plan = plan_entry_revision(
        session_factory,
        raw_message_id=ids[0],
        strategy_thread_id=thread.id,
        entry_strategy_assembly_id=ids[2],
        mode=mode,
        planned_at=NOW,
    )
    return plan.batch_id


class FakeRevisionClient:
    def __init__(self, *, cancel_error=False, wrong_replacement=False):
        self.pending = {
            "old-0": {"ordId": "old-0", "clOrdId": "old-client-0", "state": "live"},
            "old-1": {"ordId": "old-1", "clOrdId": "old-client-1", "state": "live"},
        }
        self.history = {}
        self.events = []
        self.cancel_error = cancel_error
        self.wrong_replacement = wrong_replacement

    def list_trigger_orders_pending(self, *, inst_id):
        self.events.append(("read_pending", inst_id))
        return list(self.pending.values())

    def list_trigger_order_history(self, *, inst_id):
        self.events.append(("read_history", inst_id))
        return list(self.history.values())

    def cancel_trigger_order(self, payload):
        order_id = payload.get("ordId")
        if not order_id:
            client_id = payload["clOrdId"]
            order_id = next(
                key
                for key, row in self.pending.items()
                if row.get("clOrdId") == client_id
            )
        self.events.append(("cancel", order_id))
        if self.cancel_error:
            raise TimeoutError("unknown")
        row = self.pending.pop(order_id)
        self.history[order_id] = {**row, "state": "cancelled"}
        return {"code": "0", "data": {"ordId": order_id}}

    def trigger_order(self, payload):
        client_id = payload["clOrdId"]
        self.events.append(("submit", client_id))
        order_id = f"replacement-{client_id}"
        stored = dict(payload)
        if self.wrong_replacement:
            stored["price"] = "1"
        self.pending[order_id] = {
            **stored,
            "ordId": order_id,
            "clOrdId": client_id,
            "state": "live",
        }
        return {"code": "0", "data": {"ordId": order_id}}


class FakePartialFillClient(FakeRevisionClient):
    def __init__(self, *, with_stop=True):
        super().__init__()
        self.with_stop = with_stop
        self.position_size = "0.012"

    def cancel_trigger_order(self, payload):
        order_id = payload["ordId"]
        self.events.append(("cancel", order_id))
        row = self.pending.pop(order_id)
        if order_id == "old-0":
            self.history[order_id] = {
                **row,
                "state": "filled",
                "posId": "pos-1",
            }
        else:
            self.history[order_id] = {**row, "state": "cancelled"}
        return {"code": "0", "data": {"ordId": order_id}}

    def list_positions(self, *, inst_id=None):
        self.events.append(("read_position", self.position_size))
        return [
            {
                "posId": "pos-1",
                "pos": self.position_size,
                "avgPx": "64000",
                "posSide": "short",
            }
        ]

    def read_entry_revision_stop(self, *, pos_id, inst_id):
        self.events.append(("read_stop", pos_id))
        if not self.with_stop:
            return None
        return {
            "posId": pos_id,
            "instId": inst_id,
            "purpose": "stop_loss",
            "ordId": "stop-pos-1",
            "posSide": "short",
            "triggerPrice": "65000",
            "sz": self.position_size,
            "status": "verified",
        }


class FakeChangingHeadroomClient(FakePartialFillClient):
    def __init__(self):
        super().__init__()
        self.position_size = "0.006"
        self.position_reads = 0

    def list_positions(self, *, inst_id=None):
        self.position_reads += 1
        if self.position_reads > 1:
            self.position_size = "0.007"
        return super().list_positions(inst_id=inst_id)


class FakeTakeProfitOnlyClient(FakePartialFillClient):
    def read_entry_revision_stop(self, *, pos_id, inst_id):
        row = super().read_entry_revision_stop(pos_id=pos_id, inst_id=inst_id)
        return {**row, "purpose": "take_profit"}


class FakeDeepcoinFullPositionStopClient(FakePartialFillClient):
    def read_entry_revision_stop(self, *, pos_id, inst_id):
        self.events.append(("read_stop", pos_id))
        return {
            "posId": pos_id,
            "instId": inst_id,
            "posSide": "short",
            "triggerOrderType": "TPSL",
            "slTriggerPx": "65000",
            "sz": "0",
            "ordId": "stop-pos-1",
            "status": "verified",
        }


class FakeWrongDirectionClient(FakeRevisionClient):
    def trigger_order(self, payload):
        response = super().trigger_order(payload)
        order_id = response["data"]["ordId"]
        self.pending[order_id]["side"] = (
            "sell" if payload["side"] == "buy" else "buy"
        )
        return response

def test_entry_revision_cancels_reads_back_then_rebuilds(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "execute.db")
    batch_id = _planned_batch(session_factory)
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "succeeded"
    actions = [event[0] for event in client.events]
    assert max(i for i, action in enumerate(actions) if action == "cancel") < min(
        i for i, action in enumerate(actions) if action == "submit"
    )
    with session_factory() as session:
        assert session.get(StrategyRevisionBatch, batch_id).status == "succeeded"
        legs = (
            session.query(ExecutionOrderLeg)
            .order_by(ExecutionOrderLeg.leg_index.asc())
            .all()
        )
    assert len(legs) == 4
    assert all(leg.client_order_id.startswith("ER") for leg in legs[2:])
    assert {leg.client_order_id for leg in legs[:2]}.isdisjoint(
        {leg.client_order_id for leg in legs[2:]}
    )


def test_entry_revision_wrong_geometry_is_rejected_before_authority_or_exchange(
    tmp_path,
):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "geometry-reject.db")
    batch_id = _planned_batch(session_factory)
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        replacement = json.loads(batch.replacement_json)
        replacement["stop_loss"] = 63000
        batch.replacement_json = json.dumps(replacement, sort_keys=True)
        session.commit()
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_price_geometry_stop_side_invalid"
    assert client.events == []
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        assert batch.status == "recovery_required"
        assert batch.advance_claim_token is None


def test_entry_revision_does_not_overwrite_conflicting_leg_direction(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "direction-conflict.db")
    batch_id = _planned_batch(session_factory)
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        replacement = json.loads(batch.replacement_json)
        replacement["order_legs"][0]["position_side"] = "long"
        replacement["order_legs"][0]["side"] = "buy"
        batch.replacement_json = json.dumps(replacement, sort_keys=True)
        session.commit()
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert client.events == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("take_profit_legs", {}),
        ("position_side", "long"),
        ("open_side", "buy"),
    ],
)
def test_entry_revision_rejects_malformed_tp_or_conflicting_top_direction(
    tmp_path, field, value
):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / f"revision-{field}.db")
    batch_id = _planned_batch(session_factory)
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        replacement = json.loads(batch.replacement_json)
        replacement[field] = value
        batch.replacement_json = json.dumps(replacement, sort_keys=True)
        session.commit()
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_price_geometry_ambiguous"
    assert client.events == []


def test_entry_revision_worker_advances_durable_live_batch(tmp_path):
    from telegram_kol_research.entry_revision_executor import (
        run_entry_revision_worker_once,
    )

    session_factory = create_session_factory(tmp_path / "worker.db")
    batch_id = _planned_batch(session_factory)
    client = FakeRevisionClient()

    result = run_entry_revision_worker_once(
        session_factory,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result == {"status": "completed", "batch_ids": [batch_id]}


def test_cancellation_authority_blocks_revision_before_batch_claim(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        acquire_entry_revision_exchange_authority,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "authority-busy.db")
    batch_id = _planned_batch(session_factory)
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
        owner_kind="entry_revision_worker",
        owner_id="order:reviewed-1",
        acquired_at=NOW,
    )
    assert cancellation.acquired is True
    save_trading_settings(
        session_factory,
        {"entry_revision_v2_mode": "live"},
        updated_at=NOW + timedelta(seconds=1),
    )
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW + timedelta(seconds=2),
    )

    assert result.status == "in_progress"
    assert result.reason_code == "entry_revision_exchange_authority_busy"
    assert client.events == []
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        assert batch.status == "planned"
        assert batch.advance_claim_token is None


def test_live_revision_owns_authority_during_exchange_and_releases_on_return(
    tmp_path,
):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "authority-owned.db")
    batch_id = _planned_batch(session_factory)

    class AuthorityInspectingClient(FakeRevisionClient):
        def cancel_trigger_order(self, payload):
            with session_factory() as session:
                row = (
                    session.query(TradingSetting)
                    .filter(
                        TradingSetting.key
                        == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
                    )
                    .one()
                )
                document = json.loads(row.value_json)
            assert document["state"] == "held"
            assert document["owner_kind"] == "entry_revision_worker"
            assert document["action_id"] == f"batch:{batch_id}"
            return super().cancel_trigger_order(payload)

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=AuthorityInspectingClient(),
        executed_at=NOW,
    )

    assert result.status == "succeeded"
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        assert json.loads(row.value_json)["state"] == "idle"


def test_unhandled_revision_exception_retains_exchange_authority(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "authority-retained.db")
    batch_id = _planned_batch(session_factory)

    class ExplodingReadClient(FakeRevisionClient):
        def list_trigger_orders_pending(self, *, inst_id):
            raise RuntimeError("unexpected read failure")

    try:
        execute_entry_revision(
            session_factory,
            batch_id=batch_id,
            deepcoin_client=ExplodingReadClient(),
            executed_at=NOW,
        )
    except RuntimeError as exc:
        assert str(exc) == "unexpected read failure"
    else:
        raise AssertionError("revision exception must escape")

    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        document = json.loads(row.value_json)
    assert document["state"] == "held"
    assert document["owner_kind"] == "entry_revision_worker"
    assert document["action_id"] == f"batch:{batch_id}"


def test_disabled_and_shadow_revision_do_not_acquire_exchange_authority(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    for mode in ("disabled", "shadow"):
        session_factory = create_session_factory(tmp_path / f"{mode}.db")
        batch_id = _planned_batch(
            session_factory,
            mode="live" if mode == "disabled" else "shadow",
        )
        if mode == "disabled":
            save_trading_settings(
                session_factory,
                {"entry_revision_v2_mode": "disabled"},
                updated_at=NOW + timedelta(seconds=1),
            )

        result = execute_entry_revision(
            session_factory,
            batch_id=batch_id,
            deepcoin_client=FakeRevisionClient(),
            executed_at=NOW,
        )

        assert result.status == (
            "disabled" if mode == "disabled" else "shadow_planned"
        )
        with session_factory() as session:
            row = (
                session.query(TradingSetting)
                .filter(
                    TradingSetting.key
                    == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
                )
                .one()
            )
            assert json.loads(row.value_json)["state"] == "idle"


def test_live_activation_does_not_replay_older_pending_fragments(tmp_path):
    from telegram_kol_research.entry_revision_executor import (
        run_entry_revision_worker_once,
    )

    session_factory = create_session_factory(tmp_path / "activation-cutoff.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=201,
            message_id=2002,
            posted_at=NOW - timedelta(hours=1),
            text="old half",
        )
        session.add(raw)
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=raw.id,
            version=1,
            input_fingerprint="old-fragment",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.flush()
        fragment = EntryStrategyFragment(
            raw_message_id=raw.id,
            chat_id=201,
            message_id=2002,
            symbol="BTC",
            side="short",
            fragment_kind="risk_multiplier",
            payload_json='{"risk_multiplier":"0.5"}',
            evidence_version_id=evidence.id,
            recognition_generation="old-fragment",
            source_relationship="unresolved",
            status="pending",
            reason="old",
            fingerprint="9" * 64,
            created_at=NOW + timedelta(minutes=1),
            updated_at=NOW + timedelta(minutes=1),
        )
        session.add(fragment)
        session.commit()
    save_trading_settings(
        session_factory,
        {"entry_revision_v2_mode": "live"},
        updated_at=NOW,
    )

    result = run_entry_revision_worker_once(
        session_factory,
        deepcoin_client=FakeRevisionClient(),
        executed_at=NOW,
    )

    assert result == {"status": "completed", "batch_ids": []}
    with session_factory() as session:
        assert session.query(EntryStrategyFragment).one().status == "pending"


def test_live_activation_retires_prior_shadow_batches_without_execution(tmp_path):
    from telegram_kol_research.entry_revision_executor import (
        run_entry_revision_worker_once,
    )

    session_factory = create_session_factory(tmp_path / "retire-shadow.db")
    batch_id = _planned_batch(session_factory, mode="shadow")
    save_trading_settings(
        session_factory,
        {"entry_revision_v2_mode": "live"},
        updated_at=NOW + timedelta(hours=1),
    )
    client = FakeRevisionClient()

    result = run_entry_revision_worker_once(
        session_factory,
        deepcoin_client=client,
        executed_at=NOW + timedelta(hours=1),
    )

    assert result == {"status": "completed", "batch_ids": []}
    assert client.events == []
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        assert batch.status == "blocked"
        assert batch.reason_code == "entry_revision_shadow_generation_retired"


def test_entry_revision_unknown_cancel_is_recovery_required(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "unknown.db")
    batch_id = _planned_batch(session_factory)
    client = FakeRevisionClient(cancel_error=True)

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_cancel_outcome_unknown"
    assert not any(event[0] == "submit" for event in client.events)
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        authority = json.loads(row.value_json)
    assert authority["state"] == "held"
    assert authority["owner_kind"] == "entry_revision_worker"


def test_entry_revision_postwrite_mismatch_retains_exchange_authority(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "postwrite-mismatch.db")
    batch_id = _planned_batch(session_factory)

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=FakeRevisionClient(wrong_replacement=True),
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_replacement_economics_mismatch"
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        authority = json.loads(row.value_json)
    assert authority["state"] == "held"
    assert authority["owner_kind"] == "entry_revision_worker"


def test_entry_revision_claim_lost_after_verified_writes_retains_authority(
    tmp_path,
):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "claim-lost.db")
    batch_id = _planned_batch(session_factory)

    class ClaimLosingClient(FakeRevisionClient):
        claim_lost = False

        def list_trigger_orders_pending(self, *, inst_id):
            rows = super().list_trigger_orders_pending(inst_id=inst_id)
            if not self.claim_lost and any(
                str(row.get("ordId") or "").startswith("replacement-")
                for row in rows
            ):
                with session_factory() as session:
                    batch = session.get(StrategyRevisionBatch, batch_id)
                    batch.advance_claim_token = "different-worker-token"
                    session.commit()
                self.claim_lost = True
            return rows

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=ClaimLosingClient(),
        executed_at=NOW,
    )

    assert result.status == "in_progress"
    assert result.reason_code == "entry_revision_claim_lost"
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        authority = json.loads(row.value_json)
    assert authority["state"] == "held"
    assert authority["owner_kind"] == "entry_revision_worker"


def test_entry_revision_cancels_exact_client_id_when_order_id_is_absent(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "client-id.db")
    batch_id = _planned_batch(session_factory)
    with session_factory() as session:
        first = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.leg_index == 0)
            .one()
        )
        first.order_id = None
        batch = session.get(StrategyRevisionBatch, batch_id)
        snapshot = json.loads(batch.target_snapshot_json)
        snapshot["entry_legs"][0]["order_id"] = None
        batch.target_snapshot_json = json.dumps(snapshot, sort_keys=True)
        revision_leg = (
            session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.execution_order_leg_id == first.id)
            .one()
        )
        revision_leg.order_id = None
        session.commit()
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "succeeded"


def test_entry_revision_wrong_replacement_economics_requires_recovery(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "economics.db")
    batch_id = _planned_batch(session_factory)
    client = FakeRevisionClient(wrong_replacement=True)

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_replacement_economics_mismatch"


def test_entry_revision_wrong_replacement_direction_requires_recovery(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "wrong-direction.db")
    batch_id = _planned_batch(session_factory)

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=FakeWrongDirectionClient(),
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_replacement_economics_mismatch"


def test_shadow_entry_revision_performs_zero_exchange_calls(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "shadow.db")
    batch_id = _planned_batch(session_factory, mode="shadow")
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "shadow_planned"
    assert client.events == []


def test_disabling_revision_after_planning_blocks_all_exchange_calls(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "disabled-after-plan.db")
    batch_id = _planned_batch(session_factory)
    save_trading_settings(
        session_factory,
        {"entry_revision_v2_mode": "disabled"},
        updated_at=NOW,
    )
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "disabled"
    assert client.events == []


def test_entry_revision_blocks_local_identity_drift_before_exchange_write(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "target-drift.db")
    batch_id = _planned_batch(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).first()
        leg.client_order_id = "reconciled-owner"
        session.commit()
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_frozen_target_drift"
    assert client.events == []


def test_entry_revision_reclaims_stale_prewrite_claim_after_restart(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "restart.db")
    batch_id = _planned_batch(session_factory)
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        batch.advance_claim_token = "stale-worker"
        batch.advance_claimed_at = NOW - timedelta(minutes=6)
        session.commit()
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "succeeded"


def test_partial_fill_cancels_all_pending_before_exact_risk_reduction(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "partial-fill.db")
    batch_id = _planned_batch(session_factory)
    client = FakePartialFillClient()

    def reduce_exact_position(**kwargs):
        client.events.append(("reduce", kwargs["exact_close_quantity"]))
        assert kwargs["pos_id"] == "pos-1"
        client.position_size = kwargs["target_remaining_quantity"]
        return {"status": "succeeded"}

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        risk_reduction_executor=reduce_exact_position,
        contract_value="1",
        quantity_step="0.001",
        executed_at=NOW,
    )

    assert result.status == "succeeded"
    actions = [event[0] for event in client.events]
    assert max(i for i, action in enumerate(actions) if action == "cancel") < actions.index(
        "reduce"
    )
    assert "submit" not in actions
    with session_factory() as session:
        snapshot = json.loads(
            session.get(StrategyRevisionBatch, batch_id).market_snapshot_json
        )
    assert snapshot["risk_decision"]["action"] == "reduce_to_target"


def test_partial_fill_without_verified_stop_never_rebuilds(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "missing-stop.db")
    batch_id = _planned_batch(session_factory)
    client = FakePartialFillClient(with_stop=False)

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        risk_reduction_executor=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("must not reduce without protection")
        ),
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_verified_stop_missing"
    assert not any(event[0] in {"reduce", "submit"} for event in client.events)
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        assert json.loads(row.value_json)["state"] == "held"


def test_headroom_change_at_replacement_boundary_fails_closed(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    )
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "headroom-drift.db")
    batch_id = _planned_batch(session_factory)
    client = FakeChangingHeadroomClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        contract_value="1",
        quantity_step="0.001",
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_state_changed_before_rebuild"
    assert not any(event[0] == "submit" for event in client.events)
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(
                TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY
            )
            .one()
        )
        assert json.loads(row.value_json)["state"] == "held"


def test_partial_fill_take_profit_is_not_accepted_as_stop(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "tp-is-not-stop.db")
    batch_id = _planned_batch(session_factory)
    client = FakeTakeProfitOnlyClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        contract_value="0.1",
        quantity_step="0.001",
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == "entry_revision_verified_stop_missing"
    assert not any(event[0] == "submit" for event in client.events)


def test_partial_fill_accepts_deepcoin_full_position_stop_schema(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "deepcoin-full-stop.db")
    batch_id = _planned_batch(session_factory)
    client = FakeDeepcoinFullPositionStopClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        contract_value="0.1",
        quantity_step="0.001",
        risk_reduction_executor=lambda **_: {"status": "succeeded"},
        executed_at=NOW,
    )

    assert result.status == "succeeded"
    with session_factory() as session:
        quantities = [
            json.loads(row.desired_json)["quantity"]
            for row in session.query(EntryRevisionReplacement)
            .order_by(EntryRevisionReplacement.leg_index)
            .all()
        ]
    assert quantities == [0.004, 0.002]


def test_market_replacement_is_never_submitted_without_protected_path(tmp_path):
    from telegram_kol_research.entry_revision_executor import execute_entry_revision

    session_factory = create_session_factory(tmp_path / "market-protection.db")
    batch_id = _planned_batch(session_factory)
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        replacement = json.loads(batch.replacement_json)
        replacement["order_legs"][0]["order_type"] = "market"
        batch.replacement_json = json.dumps(replacement, sort_keys=True)
        session.commit()
    client = FakeRevisionClient()

    result = execute_entry_revision(
        session_factory,
        batch_id=batch_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result.status == "recovery_required"
    assert result.reason_code == (
        "entry_revision_market_replacement_requires_protected_path"
    )
    assert not any(event[0] == "submit" for event in client.events)
