"""Phase 6k: the three conditions the per-id gates never had to state.

An enumeration of position ids carries "a person looked at this one" implicitly.
A predicate has to carry it explicitly, so the cases that matter here are the
refusals -- and each of them is a position that the original source-shaped
proposal would have released:

* a ``notify_only`` group. Its positions can satisfy both source predicates and
  the user set that group up precisely so that nothing acts in it;
* an unverified attribution. Every downstream write keys off ``pos_id``, and an
  unverified leg's ``pos_id`` is a guess;
* a blacklisted id, which must be held whatever the predicate says.

And one that is easy to forget: an **unreadable** group mode. It is not a
missing feature, it is the ordinary case when a chat is not in the config, and
treating it as permission would release on a guess.
"""

import pytest

from telegram_kol_research.source_release import (
    ADOPTED_SOURCE,
    LIMIT_ENTRY_KIND,
    SOURCE_RELEASE_BLOCKED_POS_IDS,
    describe_source_release,
    evaluate_source_release,
    resolve_group_trading_mode,
)


POS = "1001125231241107"


def _backup(**kwargs):
    params = {
        "pos_id": POS,
        "kind": "adopted_primary_backup_stop",
        "evidence_source": ADOPTED_SOURCE,
        "attribution_status": "verified",
        "group_trading_mode": "auto_trade",
    }
    params.update(kwargs)
    return evaluate_source_release(**params)


def _take_profit(**kwargs):
    params = {
        "pos_id": POS,
        "kind": "take_profit_limit_entry",
        "entry_order_kind": LIMIT_ENTRY_KIND,
        "attribution_status": "verified",
        "group_trading_mode": "auto_trade",
    }
    params.update(kwargs)
    return evaluate_source_release(**params)


def test_an_adopted_primary_in_a_trading_group_is_released():
    verdict = _backup()

    assert verdict.released
    assert verdict.reason == "released_by_source"
    assert verdict.pos_id == POS


def test_a_limit_entry_in_a_trading_group_is_released():
    verdict = _take_profit()

    assert verdict.released
    assert verdict.reason == "released_by_source"


@pytest.mark.parametrize("evaluate", [_backup, _take_profit])
def test_a_notify_only_group_is_not_released(evaluate):
    """The condition the source predicate alone does not carry.

    ``evidence_source`` and ``order_kind`` describe the *order*. Neither says
    anything about who opened the position or in which group, so without this
    the predicate would write into a group the user set up to be watched only.
    """

    verdict = evaluate(group_trading_mode="notify_only")

    assert not verdict.released
    assert verdict.reason == "group_not_auto_trade"


@pytest.mark.parametrize("evaluate", [_backup, _take_profit])
@pytest.mark.parametrize("mode", [None, "", "   "])
def test_an_unreadable_group_mode_is_not_released(evaluate, mode):
    """Unknown is a refusal with its own name, not a fallback to the default.

    This is the ordinary case for a chat that is not in the config at all, so
    it is far more likely than the notify_only case above -- and the two must
    not share a reason code, because one means "configured not to trade" and
    the other means "we do not know".
    """

    verdict = evaluate(group_trading_mode=mode)

    assert not verdict.released
    assert verdict.reason == "group_trading_mode_unknown"


@pytest.mark.parametrize("evaluate", [_backup, _take_profit])
@pytest.mark.parametrize(
    "attribution", ["unassigned", "unverified", "attribution_conflict", "", None]
)
def test_an_unverified_attribution_is_not_released(evaluate, attribution):
    """Everything downstream keys off pos_id, so an unverified one is a guess."""

    verdict = evaluate(attribution_status=attribution)

    assert not verdict.released
    assert verdict.reason == "attribution_not_verified"


@pytest.mark.parametrize("evaluate", [_backup, _take_profit])
def test_a_blocked_pos_id_is_held_whatever_the_predicate_says(evaluate):
    """The lever for an hour when a deploy is not possible.

    Asserted against a position that otherwise satisfies every condition, so
    the blacklist is shown to override a release rather than merely to agree
    with a refusal that was happening anyway.
    """

    assert evaluate().released  # the same inputs, released, one line above

    verdict = evaluate(blocked_pos_ids=frozenset({POS}))

    assert not verdict.released
    assert verdict.reason == "pos_id_blocked"


def test_the_blacklist_is_empty_in_production():
    """An id here should always come with a line saying when it goes away."""

    assert SOURCE_RELEASE_BLOCKED_POS_IDS == frozenset()


def test_a_primary_stop_this_system_submitted_is_not_the_adopted_case():
    """The source predicate still has to mean something on its own."""

    verdict = _backup(evidence_source="position_mutation_intent_readback")

    assert not verdict.released
    assert verdict.reason == "primary_stop_not_adopted"


@pytest.mark.parametrize("kind", ["trigger_limit", "market", "", None])
def test_a_non_limit_entry_is_not_the_take_profit_case(kind):
    """A-15-1 withheld plain limit entries; the other kinds were never held."""

    verdict = _take_profit(entry_order_kind=kind)

    assert not verdict.released
    assert verdict.reason == "entry_not_a_plain_limit"


def test_an_unrecognised_kind_is_not_released():
    """A third write path must be a deliberate edit, not an inherited permission."""

    verdict = evaluate_source_release(
        pos_id=POS,
        kind="some_future_path",
        evidence_source=ADOPTED_SOURCE,
        entry_order_kind=LIMIT_ENTRY_KIND,
        attribution_status="verified",
        group_trading_mode="auto_trade",
    )

    assert not verdict.released
    assert verdict.reason == "unknown_release_kind"


def test_a_missing_pos_id_is_not_released():
    assert not evaluate_source_release(pos_id="", kind="adopted_primary_backup_stop").released
    assert (
        evaluate_source_release(pos_id=None, kind="take_profit_limit_entry").reason
        == "no_pos_id"
    )


def test_the_mode_resolver_collapses_every_unknown_to_the_same_empty_string():
    """Three different causes, one consequence, deliberately not distinguished."""

    def raises(chat_id):
        raise RuntimeError("config unavailable")

    assert resolve_group_trading_mode(None, 123) == ""
    assert resolve_group_trading_mode(lambda chat: "auto_trade", None) == ""
    assert resolve_group_trading_mode(raises, 123) == ""
    assert resolve_group_trading_mode(lambda chat: None, 123) == ""
    # And a real answer survives, normalised.
    assert resolve_group_trading_mode(lambda chat: "AUTO_TRADE", 123) == "auto_trade"


def test_the_report_states_conditions_rather_than_the_word_all():
    """A gate with no list to show must show what it requires instead.

    "all" would be the least informative possible line about the widest
    possible permission -- the opposite of what the gate report exists for.
    """

    described = describe_source_release()

    for key in ("adopted_primary_backup_stop", "take_profit_limit_entry"):
        assert "auto_trade" in described[key]
        assert "verified" in described[key]
        assert described[key] != "all"
    assert described["blocked_pos_ids"] == []


def test_the_provider_reaches_the_gates_from_the_web_app():
    """The wiring, not the predicate. Six hops, and any one losing it holds everything.

    A missing provider fails closed -- nothing is released -- so this cannot
    break anything; it can only make the whole of phase 6k quietly do nothing,
    which is precisely what phase 6i cost when a default nobody thought about
    made a correct rule unreachable. Mutation-checked: deleting the provider
    from the web app's reconcile call left every other test in this file and in
    both executors' files green.

    Asserted at the source rather than by running the loop because the chain
    crosses an asyncio task, a thread handoff and two settings reads; a test
    that started all of that would be testing the runtime, not the wiring.
    """

    import inspect
    import pathlib
    import re

    from telegram_kol_research import (
        execution_bindings,
        strategy_management_worker,
        trigger_backup_stop_executor,
        trigger_take_profit_convergence_executor,
        web_app,
    )

    PARAM = "group_trading_mode_provider"

    # 1. Every function on the chain must ACCEPT it.
    for func in (
        trigger_backup_stop_executor.submit_verified_trigger_backup_stops,
        trigger_backup_stop_executor._plan_submission,
        trigger_take_profit_convergence_executor.execute_trigger_take_profit_convergence,
        trigger_take_profit_convergence_executor.execute_ready_trigger_take_profit_convergences,
        execution_bindings.reconcile_deepcoin_execution_bindings,
        strategy_management_worker.run_strategy_management_worker_tick,
        strategy_management_worker.run_strategy_management_worker_loop,
        web_app.run_deepcoin_execution_reconcile_loop,
    ):
        assert PARAM in inspect.signature(func).parameters, func.__qualname__

    # 2. And the web app must SUPPLY it -- once for each loop that reaches a
    #    gate. Accepting it everywhere while nobody passes it is the exact
    #    shape this test exists for.
    source = pathlib.Path(web_app.__file__).read_text(encoding="utf-8")
    supplied = len(
        re.findall(rf"{PARAM}=lambda chat: _group_trading_mode\(", source)
    )
    assert supplied >= 4, (
        "the web app must pass a group trading mode provider to every loop "
        f"that can reach a release gate; found {supplied}"
    )
