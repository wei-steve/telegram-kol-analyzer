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
    # And the module is actually reporting four things, not zero: a report that
    # names nothing would satisfy the subtraction above trivially.
    assert len(current_release_gates()["gates"]) == len(declared)


def test_every_gate_says_what_releasing_it_permits():
    """A position id appearing is only reviewable if the row says what it allows."""

    gates = current_release_gates()["gates"]
    assert set(gates) == set(GATE_DESCRIPTIONS)
    for name, gate in gates.items():
        assert gate["permits"], name
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
    monkeypatch.setattr(
        release_gates,
        "TAKE_PROFIT_LIMIT_ENTRY_RELEASED_POS_IDS",
        frozenset(),
    )

    text = release_gate_fingerprint()

    assert "break_even_full_exit=pos-9" in text
    # An empty gate is still named, with "-" for its contents. "Absent from the
    # line" and "released nothing" must not look the same -- a gate that
    # silently dropped out of the report is the failure this module exists to
    # prevent, and it would read as reassuring.
    assert "take_profit_limit_entry=-" in text


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
