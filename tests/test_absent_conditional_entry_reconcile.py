"""Phase 6i through the real reconciler, not just the decision function.

The decision is tested on its own in ``test_absent_conditional_entry.py``. What
these add is the wiring, which is where the original defect actually lived: the
rule was never wrong, it was simply never reached, because ``pending`` was
missing from a set two lines away from the branch that mattered.

So each test below asserts on the **persisted leg** after a real
``reconcile_deepcoin_execution_bindings`` round, and the paired refusals run the
identical fixture with one thing changed.
"""

from datetime import UTC, datetime, timedelta

from telegram_kol_research.absent_conditional_entry import (
    HISTORY_SEARCH_MAX_PAGES,
    INCIDENT_TYPE,
    TERMINAL_REASON,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    reconcile_deepcoin_execution_bindings,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RuntimeIncident,
)


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
ORDER = "1001125122023573"


class _Client:
    """Nothing on the exchange: no positions, no pending orders, no history.

    ``find_trigger_order_history_rows`` is the only interesting answer, and it
    is supplied per test as the pair the real client returns.
    """

    def __init__(self, *, matches=(), searched_to_the_end=True, pending_error=None):
        self.matches = list(matches)
        self.searched_to_the_end = searched_to_the_end
        self.pending_error = pending_error
        self.finder_calls = []

    def list_positions(self, *, inst_id=None):
        return []

    def list_open_orders(self, *, inst_id=None):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        if self.pending_error is not None:
            raise self.pending_error
        return []

    def read_trigger_orders_pending(self, *, inst_id):
        return {
            "code": "0",
            "data": self.list_trigger_orders_pending(inst_id=inst_id),
        }

    def list_order_history(self, *, inst_id=None):
        return []

    def read_order_history(self, *, inst_id=None):
        return {"code": "0", "data": []}

    def list_trade_fills(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id=None):
        return []

    def read_trigger_order_history(self, *, inst_id=None):
        return {"code": "0", "data": []}

    def find_trigger_order_history_rows(self, *, inst_id, order_id, max_pages=5):
        # ``max_pages`` mirrors the real client, default included: a stub
        # without it cannot see the budget, and the budget is what stopped the
        # deployed sweep from ever finishing its search.
        self.finder_calls.append((inst_id, order_id, max_pages))
        return list(self.matches), self.searched_to_the_end


def _seed(tmp_path, *, submitted_at):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=-1002805019371, message_id=1548, symbol="BTC",
            side="short", venue="deepcoin", margin_mode="cross",
            position_mode="split", status="open",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=2, purpose="entry",
            order_kind="trigger_limit", venue="deepcoin", status="pending",
            order_id=ORDER, client_order_id="TKDPL1548E2",
            strategy_instance_id="deepcoin:-1002805019371:1548:BTC:short",
            attribution_status="unassigned",
            request={"instId": INST, "posSide": "short", "sz": "2.0",
                     "triggerPrice": "79410.0", "slTriggerPx": "76000.0"},
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.created_at = submitted_at
        leg.updated_at = submitted_at
        session.commit()
    return session_factory, binding_id, leg_id


def _run(session_factory, client):
    return reconcile_deepcoin_execution_bindings(
        session_factory, client=client, recovered_at=NOW
    )


def _leg(session_factory, leg_id):
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        return str(leg.status or ""), str(leg.terminal_reason or "")


def test_an_absent_week_old_conditional_entry_is_collected(tmp_path):
    """Leg 582's shape exactly: accepted a week ago, now nowhere on the venue."""

    session_factory, binding_id, leg_id = _seed(
        tmp_path, submitted_at=NOW - timedelta(days=7)
    )
    client = _Client(matches=(), searched_to_the_end=True)

    _run(session_factory, client)

    assert _leg(session_factory, leg_id) == ("exchange_cancelled", TERMINAL_REASON)
    # It looked up the right order on the instrument the request names.
    assert client.finder_calls == [(INST, ORDER, HISTORY_SEARCH_MAX_PAGES)]
    # And the binding follows, because every entry leg is now terminal. This is
    # the second half of the defect: without it the binding stays "open" even
    # once the leg is collected.
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        assert binding.status == "closed"
        assert binding.last_exchange_status == "entry_legs_terminal"


def test_the_collection_raises_an_always_notified_incident(tmp_path):
    """Our books and the venue disagreed; tidying one side is not the whole job."""

    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    session_factory, _, _ = _seed(tmp_path, submitted_at=NOW - timedelta(days=7))

    _run(session_factory, _Client(matches=(), searched_to_the_end=True))

    with session_factory() as session:
        incidents = (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == INCIDENT_TYPE)
            .all()
        )
    assert len(incidents) == 1
    assert ORDER in str(incidents[0].redacted_summary)
    assert INCIDENT_TYPE in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_a_page_budget_that_ran_out_leaves_the_leg_pending(tmp_path):
    """Same fixture, one thing changed: the search did not finish.

    The pair is the assertion. An empty history and an unfinished search look
    identical from the row alone.
    """

    session_factory, binding_id, leg_id = _seed(
        tmp_path, submitted_at=NOW - timedelta(days=7)
    )

    _run(session_factory, _Client(matches=(), searched_to_the_end=False))

    assert _leg(session_factory, leg_id) == ("pending", "")
    with session_factory() as session:
        assert session.get(ExecutionBinding, binding_id).status != "closed"
        assert (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == INCIDENT_TYPE)
            .count()
            == 0
        )


def test_the_reconciler_actually_prints_the_hold(tmp_path, caplog):
    """The wiring, not the formatter.

    ``format_verdict_for_log`` is tested on its own, and that test stayed green
    when the call site was deleted -- the same shape as the defect this whole
    step is about: the rule was right and nothing reached it. So this asserts
    the line lands in the log during a real reconcile round, which is the only
    place it does anybody any good.
    """

    import logging

    session_factory, _, leg_id = _seed(
        tmp_path, submitted_at=NOW - timedelta(days=7)
    )

    with caplog.at_level(logging.INFO, logger="telegram_kol_research.execution_bindings"):
        _run(session_factory, _Client(matches=(), searched_to_the_end=False))

    lines = [r.getMessage() for r in caplog.records]
    held = [ln for ln in lines if ln.startswith("absent_conditional_entry ")]
    assert held, lines
    assert f"leg={leg_id}" in held[0]
    assert "reason=history_not_exhausted" in held[0]
    assert "searched_to_the_end=False" in held[0]


def test_an_ordinary_leg_does_not_print_every_round(tmp_path, caplog):
    """The other direction: a line per leg per round forever is not observability.

    A limit entry is not what 6i is about, so it must produce no line at all --
    otherwise the log fills with legs nobody is holding and the one leg that is
    held stops standing out.
    """

    import logging

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=-1, message_id=1, symbol="BTC", side="long",
            venue="deepcoin", margin_mode="cross", position_mode="split",
            status="open",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry",
            order_kind="limit", venue="deepcoin", status="pending",
            order_id="ordinary-1",
            strategy_instance_id="deepcoin:-1:1:BTC:long",
            attribution_status="unassigned",
            request={"instId": INST, "posSide": "long", "sz": "1"},
        ),
    )

    with caplog.at_level(logging.INFO, logger="telegram_kol_research.execution_bindings"):
        _run(session_factory, _Client(matches=(), searched_to_the_end=False))

    held = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("absent_conditional_entry ")
    ]
    assert held == []


def test_a_pending_read_error_leaves_the_leg_pending(tmp_path):
    """"Absent from pending" was never established, so nothing follows from it."""

    session_factory, _, leg_id = _seed(
        tmp_path, submitted_at=NOW - timedelta(days=7)
    )
    client = _Client(
        matches=(), searched_to_the_end=True, pending_error=RuntimeError("boom")
    )

    try:
        _run(session_factory, client)
    except Exception:
        # The live reconciler may surface an incomplete snapshot; either way
        # the leg must not have moved.
        pass

    assert _leg(session_factory, leg_id) == ("pending", "")
    # And it never went on to ask the exchange: a read that failed is not
    # something a further read can rescue.
    assert client.finder_calls == []


def test_an_order_submitted_today_is_left_alone(tmp_path):
    """A conditional entry is briefly in neither list around its own conversion."""

    session_factory, _, leg_id = _seed(
        tmp_path, submitted_at=NOW - timedelta(hours=2)
    )
    client = _Client(matches=(), searched_to_the_end=True)

    _run(session_factory, client)

    assert _leg(session_factory, leg_id) == ("pending", "")
    assert client.finder_calls == []


def test_a_leg_found_deeper_in_history_is_not_collected_by_this_path(tmp_path):
    """It was there after all; ending it on "absence" would be a false reason."""

    session_factory, _, leg_id = _seed(
        tmp_path, submitted_at=NOW - timedelta(days=7)
    )

    _run(
        session_factory,
        _Client(matches=({"ordId": ORDER, "state": "live"},), searched_to_the_end=True),
    )

    status, reason = _leg(session_factory, leg_id)
    assert reason != TERMINAL_REASON
    assert status != "exchange_cancelled"
