from threading import Event, Thread

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    reconcile_deepcoin_execution_bindings,
    sync_manual_closed_deepcoin_positions,
)
from telegram_kol_research.position_authority_lock import (
    serialized_position_authority_mutation,
)


def test_reconcile_cannot_cross_exchange_mutation_authority_boundary(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    mutation_entered = Event()
    release_mutation = Event()
    reconcile_read_exchange = Event()

    @serialized_position_authority_mutation
    def simulated_exchange_mutation():
        mutation_entered.set()
        assert release_mutation.wait(timeout=2)

    class FakeClient:
        uid_scope_hash = "5" * 64

        def list_positions(self):
            reconcile_read_exchange.set()
            return []

        def list_open_orders(self):
            return []

        def list_position_history(self, *, inst_id, pos_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_order_history(self, *, inst_id):
            return []

        def list_trade_fills(self, *, inst_id):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return []

    mutation_thread = Thread(target=simulated_exchange_mutation)
    reconcile_thread = Thread(
        target=reconcile_deepcoin_execution_bindings,
        kwargs={"session_factory": session_factory, "client": FakeClient()},
    )
    mutation_thread.start()
    assert mutation_entered.wait(timeout=1)
    reconcile_thread.start()

    assert reconcile_read_exchange.wait(timeout=0.1) is False
    release_mutation.set()
    mutation_thread.join(timeout=2)
    reconcile_thread.join(timeout=2)
    assert reconcile_read_exchange.is_set()
    assert mutation_thread.is_alive() is False
    assert reconcile_thread.is_alive() is False


def test_manual_close_sync_uses_the_same_authority_boundary(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    mutation_entered = Event()
    release_mutation = Event()
    sync_read_exchange = Event()

    @serialized_position_authority_mutation
    def simulated_exchange_mutation():
        mutation_entered.set()
        assert release_mutation.wait(timeout=2)

    class FakeClient:
        def list_positions(self):
            sync_read_exchange.set()
            return []

    mutation_thread = Thread(target=simulated_exchange_mutation)
    sync_thread = Thread(
        target=sync_manual_closed_deepcoin_positions,
        kwargs={"session_factory": session_factory, "client": FakeClient()},
    )
    mutation_thread.start()
    assert mutation_entered.wait(timeout=1)
    sync_thread.start()

    assert sync_read_exchange.wait(timeout=0.1) is False
    release_mutation.set()
    mutation_thread.join(timeout=2)
    sync_thread.join(timeout=2)
    assert sync_read_exchange.is_set()
    assert sync_thread.is_alive() is False
