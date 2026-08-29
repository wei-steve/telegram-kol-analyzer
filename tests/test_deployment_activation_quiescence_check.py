from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deployment_activation_quiescence_check import (
    ActivationQuiescenceError,
    inspect_activation_quiescence,
)
from telegram_kol_research.models import TradingSetting


def _write_authority(session_factory, document: dict) -> None:
    with session_factory() as session:
        session.add(
            TradingSetting(
                key="entry_revision_exchange_authority",
                value_json=json.dumps(document, separators=(",", ":"), sort_keys=True),
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()


def test_activation_quiescence_requires_exact_idle_durable_authority(tmp_path) -> None:
    database = tmp_path / "idle.db"
    session_factory = create_session_factory(database)
    _write_authority(
        session_factory,
        {
            "released_at": datetime.now(UTC).isoformat(),
            "generation": 0,
            "schema_version": 2,
            "state": "idle",
        },
    )

    assert inspect_activation_quiescence(database) == 0


@pytest.mark.parametrize(
    "document",
    [
        None,
        {
            "acquired_at": datetime.now(UTC).isoformat(),
            "owner_id": "unknown-attempt",
            "owner_kind": "new_entry_worker",
            "schema_version": 1,
            "state": "held",
            "token": "secret-not-reported",
        },
        {"schema_version": 1, "state": "idle", "released_at": ""},
        {
            "generation": 0,
            "released_at": datetime.now(UTC).isoformat(),
            "schema_version": 2,
            "state": "blocked",
        },
    ],
)
def test_activation_quiescence_fails_closed_for_missing_held_or_malformed_authority(
    tmp_path, document
) -> None:
    database = tmp_path / "blocked.db"
    session_factory = create_session_factory(database)
    if document is not None:
        _write_authority(session_factory, document)

    with pytest.raises(ActivationQuiescenceError, match="unknown"):
        inspect_activation_quiescence(database)
