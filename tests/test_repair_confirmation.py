from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.repair_confirmation import (
    consume_repair_confirmation_token,
)


def test_confirmation_token_is_globally_single_use_across_actions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    consume_repair_confirmation_token(
        session_factory,
        confirmation_token="one-global-token",
        action_kind="backup_stop_repair",
        action_id="a" * 64,
        pos_id="pos-1",
        consumed_at=now,
    )

    with pytest.raises(
        ValueError, match="confirmation_token already consumed"
    ):
        consume_repair_confirmation_token(
            session_factory,
            confirmation_token="one-global-token",
            action_kind="current_protection_backfill",
            action_id="b" * 64,
            pos_id="pos-2",
            consumed_at=now,
        )
