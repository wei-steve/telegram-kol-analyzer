"""Every exchange-write gate is reported, and the report follows the gates.

The point of this module is that a released gate should stop being invisible.
So the tests that matter are the ones that fail when a gate is added to the
code and not to the report -- not the ones that check today's values, which
change by design whenever a position is approved.
"""

import telegram_kol_research.release_gates as release_gates
from telegram_kol_research.release_gates import (
    GATE_DESCRIPTIONS,
    current_release_gates,
    format_release_gates_for_log,
    release_gate_fingerprint,
)


def test_every_release_constant_in_the_codebase_is_reported():
    """A gate that exists and is not reported is the defect this module fixes.

    Reads the source rather than a list, so adding a fifth
    ``*_RELEASED_POS_IDS`` constant anywhere turns this red until it is also
    surfaced. A hand-maintained list would have to be remembered, and this
    repository has written down twice that a criterion needing to be remembered
    is not a criterion.
    """

    import pathlib
    import re

    package = pathlib.Path(
        __import__("telegram_kol_research").__file__
    ).parent
    declared = set()
    for path in package.rglob("*.py"):
        if path.name == "release_gates.py":
            continue
        for match in re.finditer(
            r"^([A-Z_]*RELEASED_POS_IDS)\s*[:=]", path.read_text(encoding="utf-8"), re.M
        ):
            declared.add(match.group(1))

    reported = set()
    source = pathlib.Path(release_gates.__file__).read_text(encoding="utf-8")
    for name in declared:
        if name in source:
            reported.add(name)

    assert declared - reported == set(), (
        "these release constants exist but release_gates does not report them: "
        f"{sorted(declared - reported)}"
    )
    # And the module is actually reporting something, not nothing: a report
    # that names nothing would satisfy the subtraction above trivially.
    #
    # Phase 6k broke the old equality here, and the way it broke is the point.
    # Two gates stopped being ``*_RELEASED_POS_IDS`` constants and became a
    # predicate, so counting constants no longer counts gates -- and a report
    # that silently dropped those two would still have passed a subtraction
    # over the constants that remain. So both shapes are named explicitly.
    gates = current_release_gates()["gates"]
    assert declared <= {
        "BREAK_EVEN_REPLACEMENT_RELEASED_POS_IDS",
        "BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS",
    }, sorted(declared)
    assert set(gates) == {
        "break_even_replacement",
        "break_even_full_exit",
        "take_profit_limit_entry",
        "adopted_primary_backup_stop",
    }
    # The two predicate-shaped ones must render their conditions, not a list.
    for name in ("take_profit_limit_entry", "adopted_primary_backup_stop"):
        assert gates[name]["shape"] == "predicate"
        assert "auto_trade" in gates[name]["conditions"]
        assert "verified" in gates[name]["conditions"]


def test_every_gate_says_what_releasing_it_permits():
    """A position id appearing is only reviewable if the row says what it allows."""

    gates = current_release_gates()["gates"]
    assert set(gates) == set(GATE_DESCRIPTIONS)
    for name, gate in gates.items():
        assert gate["permits"], name
        if gate.get("shape") == "predicate":
            # No list to be consistent with; what it must not do is render as
            # "all", which would be the least informative possible line about
            # the widest possible permission.
            assert gate["conditions"] not in ("", "all"), name
            assert isinstance(gate["blocked_pos_ids"], list), name
        else:
            assert gate["count"] == len(gate["released_pos_ids"])


def test_the_fingerprint_changes_when_a_gate_changes(monkeypatch):
    """What makes it usable as an observation script's gates_ok."""

    before = release_gate_fingerprint()
    monkeypatch.setattr(
        release_gates,
        "BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS",
        frozenset({"some-position"}),
    )
    after = release_gate_fingerprint()

    assert after != before
    assert "some-position" in after


def test_the_fingerprint_names_what_changed_rather_than_hashing_it(monkeypatch):
    """A digest would say that something changed, not which gate opened.

    Same shortfall as phase 6f's first hold record, which said "held" and
    nothing else: an operator has to be able to act on it without going to
    fetch the thing it is describing.
    """

    monkeypatch.setattr(
        release_gates,
        "BREAK_EVEN_FULL_EXIT_RELEASED_POS_IDS",
        frozenset({"pos-9"}),
    )

    text = release_gate_fingerprint()

    assert "break_even_full_exit=pos-9" in text
    # An empty gate is still named, with "-" for its contents. "Absent from the
    # line" and "released nothing" must not look the same -- a gate that
    # silently dropped out of the report is the failure this module exists to
    # prevent, and it would read as reassuring.
    assert "break_even_replacement=" in text
    # And a predicate-shaped gate renders as "by-source", never as "-".
    # Rendering it as "-" would say "nothing is released", the exact opposite
    # of what it means, while an observation script comparing fingerprints went
    # on matching as the shape changed underneath it.
    assert "take_profit_limit_entry=by-source(blocked:none)" in text
    assert "adopted_primary_backup_stop=by-source(blocked:none)" in text
    assert "take_profit_limit_entry=-" not in text


def test_a_blocked_id_shows_up_in_the_fingerprint(monkeypatch):
    """The per-position lever that remains has to be visible too.

    It is the only thing that can change a predicate gate's behaviour without a
    predicate change, so an observation window must be able to notice it.
    """

    import telegram_kol_research.source_release as source_release

    monkeypatch.setattr(
        source_release, "SOURCE_RELEASE_BLOCKED_POS_IDS", frozenset({"pos-held"})
    )
    monkeypatch.setattr(
        release_gates, "SOURCE_RELEASE_BLOCKED_POS_IDS", frozenset({"pos-held"})
    )

    text = release_gate_fingerprint()

    assert "blocked:pos-held" in text


def test_the_startup_line_carries_the_fingerprint():
    line = format_release_gates_for_log()

    assert line.startswith("release_gates ")
    assert release_gate_fingerprint() in line


def test_the_module_only_reports(monkeypatch):
    """It must not decide anything: what it reports has to be what acts.

    Asserted against the source because a decision here would be invisible to
    a value check -- the module would simply report something a caller never
    consulted.
    """

    import pathlib

    source = pathlib.Path(release_gates.__file__).read_text(encoding="utf-8")
    for forbidden in ("def is_released", "def should_", "def may_", "raise "):
        assert forbidden not in source, forbidden
