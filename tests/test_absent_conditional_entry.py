"""Phase 6i: only an exhausted search may end a conditional entry.

The three that matter are the three the design turns on, and two of them are
refusals:

* the page budget ran out -- ``(matches=[], searched_to_the_end=False)`` -- and
  nothing is collected. This is the case that would be invisible without a
  test, because "not found" and "did not finish looking" are the same empty
  list;
* a snapshot read failed, so "absent from pending" was never established, and
  nothing is collected;
* the search reached the end and found nothing, and only then is the leg ended.

Every assertion below is on the *verdict*, not on the leg having been visited.
A count of what was examined is decided by this module's own filter and stays
green when the decision is wrong -- the shape that let three mutations through
on the naked-fill shadow.
"""

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.absent_conditional_entry import (
    HISTORY_SEARCH_MAX_PAGES,
    MIN_ABSENCE_AGE,
    TERMINAL_REASON,
    evaluate_absent_conditional_entry,
    instrument_id_for_leg,
    snapshot_read_failed_for,
)


NOW = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=7)
INST = "BTC-USDT-SWAP"
ORDER = "1001125122023573"


class _Leg:
    """Only the attributes the decision reads."""

    def __init__(
        self,
        *,
        status="pending",
        order_kind="trigger_limit",
        purpose="entry",
        order_id=ORDER,
        created_at=LONG_AGO,
        request_json=f'{{"instId":"{INST}","triggerPrice":"79410.0"}}',
    ):
        self.id = 582
        self.status = status
        self.order_kind = order_kind
        self.purpose = purpose
        self.order_id = order_id
        self.created_at = created_at
        self.request_json = request_json


class _Client:
    """A paging history reader, with the two answers spelled out separately.

    The signature mirrors the real client **including ``max_pages`` and its
    default of 5**, which is not decoration. The first version of this stub
    omitted the parameter, so the tests could not see the page budget at all --
    and the deployed sweep then refused every round because five pages do not
    reach the end of a thirteen-page history. A fake narrower than the thing it
    stands in for hides exactly the dimension it dropped.
    """

    def __init__(self, *, matches=(), searched_to_the_end=True, raises=False):
        self.matches = list(matches)
        self.searched_to_the_end = searched_to_the_end
        self.raises = raises
        self.calls = []

    def find_trigger_order_history_rows(self, *, inst_id, order_id, max_pages=5):
        self.calls.append((inst_id, order_id, max_pages))
        if self.raises:
            raise RuntimeError("read failed")
        return list(self.matches), self.searched_to_the_end


def _evaluate(leg=None, client=None, **kwargs):
    params = {
        "absent_from_pending": True,
        "absent_from_history_page": True,
        "client": client if client is not None else _Client(),
        "snapshot_errors": {},
        "now": NOW,
    }
    params.update(kwargs)
    return evaluate_absent_conditional_entry(leg or _Leg(), **params)


def test_an_exhausted_search_that_found_nothing_collects_the_leg():
    client = _Client(matches=(), searched_to_the_end=True)

    verdict = _evaluate(client=client)

    assert verdict.collects
    assert verdict.status == "collect"
    assert verdict.reason == TERMINAL_REASON
    assert verdict.searched_to_the_end is True
    assert verdict.history_matches == 0
    # And it asked about the right order on the right instrument. Paging some
    # other instrument's history would also return no match.
    assert client.calls == [(INST, ORDER, HISTORY_SEARCH_MAX_PAGES)]


def test_a_page_budget_that_ran_out_collects_nothing():
    """The case an empty list cannot distinguish on its own.

    ``find_trigger_order_history_rows`` returns ``([], False)`` when it stopped
    early. Reading that as absence would end an order that may still be resting
    on the exchange -- and the two answers are the same empty list, so only the
    flag separates them.
    """

    verdict = _evaluate(client=_Client(matches=(), searched_to_the_end=False))

    assert not verdict.collects
    assert verdict.reason == "history_not_exhausted"
    assert verdict.searched_to_the_end is False


def test_a_failed_history_search_collects_nothing():
    verdict = _evaluate(client=_Client(raises=True))

    assert not verdict.collects
    assert verdict.reason == "history_search_failed"


@pytest.mark.parametrize(
    "errors, expected",
    [
        ({f"pending_trigger_orders:{INST}": "boom"}, "snapshot_read_error"),
        ({f"trigger_history:{INST}": "boom"}, "snapshot_read_error"),
        ({"pending_trigger_orders": "boom"}, "snapshot_read_error"),
    ],
)
def test_a_snapshot_read_error_collects_nothing(errors, expected):
    """Hard rule 4 over a pair of reads.

    If the pending read failed, "absent from pending" was never established at
    all; if the history page read failed, the snapshot's absence is equally
    uninformed. Either one has to stop the round.
    """

    client = _Client(matches=(), searched_to_the_end=True)

    verdict = _evaluate(client=client, snapshot_errors=errors)

    assert not verdict.collects
    assert verdict.reason == expected
    # And it did not even reach the exchange: an unreadable snapshot is not
    # something a further read can rescue.
    assert client.calls == []


def test_an_error_on_another_instrument_does_not_block_this_one():
    """Otherwise one bad instrument freezes the sweep for every other.

    The paired test above is what makes this one safe to have: without it,
    scoping the error check could quietly become no error check at all.
    """

    verdict = _evaluate(snapshot_errors={"trigger_history:ETH-USDT-SWAP": "boom"})

    assert verdict.collects


def test_an_order_younger_than_the_window_collects_nothing():
    leg = _Leg(created_at=NOW - MIN_ABSENCE_AGE + timedelta(minutes=1))

    verdict = _evaluate(leg=leg)

    assert not verdict.collects
    assert verdict.reason == "too_recent"
    # One minute the other way and it would collect: the boundary is the
    # window, not some other property of this fixture.
    assert _evaluate(leg=_Leg(created_at=NOW - MIN_ABSENCE_AGE)).collects


def test_a_leg_still_on_the_pending_list_collects_nothing():
    verdict = _evaluate(absent_from_pending=False)

    assert not verdict.collects
    assert verdict.reason == "still_on_the_pending_list"


def test_a_leg_present_in_the_history_page_collects_nothing():
    verdict = _evaluate(absent_from_history_page=False)

    assert not verdict.collects
    assert verdict.reason == "present_in_history_page"


def test_a_match_found_deeper_in_history_collects_nothing():
    """It was there after all, past the snapshot's first page.

    Classifying its state belongs to the existing path, which reads the row.
    """

    verdict = _evaluate(
        client=_Client(matches=({"ordId": ORDER, "state": "canceled"},))
    )

    assert not verdict.collects
    assert verdict.reason == "present_in_exhausted_history"
    assert verdict.history_matches == 1


@pytest.mark.parametrize(
    "leg, expected",
    [
        (_Leg(status="active"), "not_pending"),
        (_Leg(status="cancelled"), "not_pending"),
        (_Leg(order_kind="limit"), "not_a_conditional_entry"),
        (_Leg(order_kind="market"), "not_a_conditional_entry"),
        (_Leg(purpose="stop_loss"), "not_an_entry_leg"),
        (_Leg(order_id=""), "no_order_id"),
        (_Leg(request_json="{}"), "no_instrument_id"),
        (_Leg(request_json="not json"), "no_instrument_id"),
        (_Leg(created_at=None), "no_submitted_at"),
    ],
)
def test_everything_else_is_a_named_hold(leg, expected):
    """Named, because "it did not collect" is not a reason anyone can act on.

    This is the same failure phase 6f recorded: a record saying only "held"
    made the approval request impossible to build without going back to the
    code.
    """

    verdict = _evaluate(leg=leg)

    assert not verdict.collects
    assert verdict.reason == expected


def test_a_client_without_the_paging_reader_collects_nothing():
    """The single-page snapshot is not a substitute, and the filtered endpoint lies.

    ``list_trigger_order_history_by_order_id`` returns ``[]`` for every order id
    (A-5b). A client that only has that one must not be allowed to look like a
    successful exhaustive search.
    """

    class _NoFinder:
        def list_trigger_order_history_by_order_id(self, *, inst_id, order_id):
            return []

    verdict = _evaluate(client=_NoFinder())

    assert not verdict.collects
    assert verdict.reason == "no_exhaustive_history_reader"


def test_the_instrument_comes_from_the_request_not_a_rebuilt_symbol():
    """A guessed instrument would page the wrong history and find nothing.

    Which this module would then read as absence -- so the guess is refused
    rather than defaulted.
    """

    assert instrument_id_for_leg(_Leg()) == INST

    class _SymbolOnly:
        request_json = None
        request = None
        symbol = "BTC"

    assert instrument_id_for_leg(_SymbolOnly()) == ""


def test_a_naive_stored_timestamp_is_read_as_utc_not_as_host_local_time(monkeypatch):
    """The mixed pair is the dangerous one, and it is the one production has.

    ``recovered_at`` arrives aware (``datetime.now(UTC)``); ``leg.created_at``
    comes back from SQLite naive. Every stored timestamp here is UTC, so the
    naive one must be *stamped* UTC. Converting it as if it were host local
    time instead shifts only that side of the subtraction -- and on the
    production host, which runs at UTC+8, it shifts the wrong way: an order 20
    hours old measures as 28 and is collected eight hours early.

    The timezone is forced rather than inherited. An earlier version of this
    test used a naive ``now`` as well, so both sides shifted together, the
    difference was unchanged, and the mutation that reads naive stamps as local
    passed it -- a test whose subject cancelled itself out.
    """

    import time

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        twenty_hours_old = (NOW - timedelta(hours=20)).replace(tzinfo=None)

        verdict = _evaluate(leg=_Leg(created_at=twenty_hours_old), now=NOW)

        assert not verdict.collects
        assert verdict.reason == "too_recent"
        assert verdict.age_seconds == 20 * 3600
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_search_gets_a_budget_far_past_the_client_default():
    """The defect that shipped: a correct refusal, forever.

    ``find_trigger_order_history_rows`` defaults to five pages. Measured against
    production on 2026-09-11, this account's whole trigger-order history was
    thirteen pages, so the search never reached the end, the module held every
    round exactly as designed, and the leg it was written for stayed pending
    with the sweep deployed and running.

    Two assertions, because they fail for different reasons: the constant must
    be larger than the client's own default (otherwise nothing changed), and it
    must clear the measured history by a real margin (otherwise it is pinned to
    one day's measurement and expires the next time the account trades).
    """

    import inspect

    from telegram_kol_research.deepcoin_client import DeepcoinRestClient

    client_default = inspect.signature(
        DeepcoinRestClient.find_trigger_order_history_rows
    ).parameters["max_pages"].default

    assert HISTORY_SEARCH_MAX_PAGES > client_default
    assert HISTORY_SEARCH_MAX_PAGES >= 13 * 2


def test_a_hold_is_printable_so_it_cannot_refuse_silently():
    """The other half of the same defect.

    The sweep spent its first ninety minutes in production refusing for a reason
    that existed nowhere outside the function that computed it -- no incident,
    no log line, nothing. "Holding because it could not finish looking" and "not
    running at all" produced identical evidence, which is none.
    """

    from telegram_kol_research.absent_conditional_entry import (
        QUIET_HOLD_REASONS,
        format_verdict_for_log,
    )

    verdict = _evaluate(client=_Client(matches=(), searched_to_the_end=False))
    line = format_verdict_for_log(verdict)

    assert "reason=history_not_exhausted" in line
    assert "searched_to_the_end=False" in line
    assert f"order={ORDER}" in line
    assert f"inst={INST}" in line
    # The reason a caller must NOT stay quiet about: it is the one that means
    # "this leg is a candidate and something stopped me".
    assert "history_not_exhausted" not in QUIET_HOLD_REASONS
    assert "snapshot_read_error" not in QUIET_HOLD_REASONS
    assert "too_recent" not in QUIET_HOLD_REASONS
    # And the ones it must stay quiet about, or it prints a line per leg per
    # round for every ordinary leg in the book.
    assert "not_a_conditional_entry" in QUIET_HOLD_REASONS
    assert "not_pending" in QUIET_HOLD_REASONS


def test_snapshot_read_failed_for_names_the_key_it_tripped_on():
    """So an operator reading a hold knows which read to go and look at."""

    message = snapshot_read_failed_for(
        {f"trigger_history:{INST}": "timeout"}, instrument_id=INST
    )

    assert f"trigger_history:{INST}" in message
    assert "timeout" in message
    assert snapshot_read_failed_for({}, instrument_id=INST) == ""
    assert snapshot_read_failed_for(None, instrument_id=INST) == ""
    # An unrelated source is not this instrument's problem.
    assert snapshot_read_failed_for({"positions": "boom"}, instrument_id=INST) == ""
