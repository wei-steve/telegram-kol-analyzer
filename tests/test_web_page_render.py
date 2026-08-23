import json
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import StaticDeepcoinContractSpecProvider
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import ContextAnalysisBackfill
from telegram_kol_research.models import ContextResolutionAttempt
from telegram_kol_research.models import MediaAsset
from telegram_kol_research.models import MessageRecognition
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import PositionTakeProfitOrder
from telegram_kol_research.models import RawMessage
from telegram_kol_research.models import RecognitionDecision
from telegram_kol_research.models import SignalCandidate
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.live_position_snapshot import LivePositionSnapshotStore
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.web_app import _summarize_verified_exchange_protection_rows
from telegram_kol_research.web_app import _load_exchange_tab_snapshot
from telegram_kol_research.web_app import HistoryPositionBrowseSnapshotStore
from telegram_kol_research.web_app import create_web_app
from telegram_kol_research.web_queries import list_exited_strategies
from telegram_kol_research.web_queries import list_verified_deepcoin_history_positions


def test_history_position_browse_snapshot_returns_stable_cursor_pages():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    store = HistoryPositionBrowseSnapshotStore(
        now_provider=lambda: now,
        token_factory=lambda: "browse-token",
    )
    rows = tuple(
        {
            "history_sort_id": f"pos-{index:03d}",
            "symbol": "BTC",
        }
        for index in range(45)
    )

    token = store.create(rows=rows, filter_key=(None, None))
    first = store.page(token=token, cursor=None, page_size=20, filter_key=(None, None))
    second = store.page(
        token=token,
        cursor="pos-019",
        page_size=20,
        filter_key=(None, None),
    )
    last = store.page(
        token=token,
        cursor="pos-039",
        page_size=20,
        filter_key=(None, None),
    )

    assert token == "browse-token"
    assert [row["history_sort_id"] for row in first.rows] == [
        f"pos-{index:03d}" for index in range(20)
    ]
    assert first.next_cursor == "pos-019"
    assert first.has_more is True
    assert [row["history_sort_id"] for row in second.rows] == [
        f"pos-{index:03d}" for index in range(20, 40)
    ]
    assert second.next_cursor == "pos-039"
    assert [row["history_sort_id"] for row in last.rows] == [
        f"pos-{index:03d}" for index in range(40, 45)
    ]
    assert last.next_cursor is None
    assert last.has_more is False


def test_history_position_browse_snapshot_rejects_expired_mismatched_or_unknown_cursor():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    store = HistoryPositionBrowseSnapshotStore(
        now_provider=lambda: now,
        token_factory=lambda: "browse-token",
        ttl=timedelta(seconds=30),
    )
    token = store.create(
        rows=({"history_sort_id": "pos-001"},),
        filter_key=("2026-08-01", "2026-08-10"),
    )

    with pytest.raises(ValueError, match="filter mismatch"):
        store.page(
            token=token,
            cursor=None,
            page_size=20,
            filter_key=(None, None),
        )
    with pytest.raises(ValueError, match="cursor"):
        store.page(
            token=token,
            cursor="unknown",
            page_size=20,
            filter_key=("2026-08-01", "2026-08-10"),
        )

    now += timedelta(seconds=31)
    with pytest.raises(ValueError, match="expired"):
        store.page(
            token=token,
            cursor=None,
            page_size=20,
            filter_key=("2026-08-01", "2026-08-10"),
        )


def test_trading_settings_page_keeps_legacy_range_controls_as_hidden_rollback_state(
    tmp_path,
):
    app = create_web_app(database_path=tmp_path / "research.db")
    save_trading_settings(
        app.state.session_factory,
        {
            "max_market_entry_deviation_pct": 0.27,
            "entry_range_order_style": "conservative",
        },
    )

    html = TestClient(app).get("/more-panel").text

    assert (
        'type="hidden" name="max_market_entry_deviation_pct" value="0.27"'
        in html
    )
    assert (
        'type="hidden" name="entry_range_order_style" value="conservative"'
        in html
    )
    assert "现价入场最大偏离 %" not in html
    assert "区间入场方式" not in html


def test_history_position_time_order_prefers_closed_time_and_uses_stable_id(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    records = [
        ("EARLY", datetime(2026, 7, 22, 8, 0, tzinfo=UTC), datetime(2026, 7, 22, 9, 0, tzinfo=UTC)),
        ("LATEST", datetime(2026, 7, 22, 10, 0, tzinfo=UTC), datetime(2026, 7, 22, 12, 0, tzinfo=UTC)),
        ("FALLBACK", datetime(2026, 7, 22, 13, 0, tzinfo=UTC), None),
        ("TIE_FIRST", datetime(2026, 7, 22, 7, 0, tzinfo=UTC), datetime(2026, 7, 22, 11, 0, tzinfo=UTC)),
        ("TIE_SECOND", datetime(2026, 7, 22, 7, 30, tzinfo=UTC), datetime(2026, 7, 22, 11, 0, tzinfo=UTC)),
    ]
    with session_factory() as session:
        for message_id, (symbol, entered_at, exited_at) in enumerate(records, start=1):
            raw_message = RawMessage(
                chat_id=100,
                message_id=message_id,
                posted_at=entered_at,
                sender_name="alice",
                text=f"{symbol} long Entry 100 SL 90 TP 110",
            )
            session.add(raw_message)
            session.flush()
            candidate = SignalCandidate(
                raw_message_id=raw_message.id,
                symbol=symbol,
                side="long",
                event_type="entry_signal",
                entry_text="100",
                stop_loss_text="90",
                take_profit_text="110",
                parse_source="text_ai",
                confidence=0.9,
                review_status="pending",
            )
            session.add(candidate)
            session.flush()
            session.add(
                StrategyLifecycle(
                    signal_candidate_id=candidate.id,
                    chat_id=100,
                    message_id=message_id,
                    symbol=symbol,
                    side="long",
                    lifecycle_status="exited",
                    exit_reason="take_profit",
                    signal_at=entered_at,
                    entered_at=entered_at,
                    exited_at=exited_at,
                    entry_price_actual=100,
                    exit_price_actual=110,
                )
            )
        session.commit()

    rows = list_exited_strategies(session_factory, limit=10)

    assert [row["history_sort_id"] for row in rows] == [
        "lifecycle:2",
        "lifecycle:5",
        "lifecycle:4",
        "lifecycle:1",
        "lifecycle:3",
    ]


def test_deepcoin_history_position_layout_uses_app_information_hierarchy(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    opened_at = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    closed_at = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=1,
            posted_at=opened_at,
            sender_name="alice",
            text="BTC short Entry 66000 SL 67000 TP 65000",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="66000",
            stop_loss_text="67000",
            take_profit_text="65000",
            parse_source="text_ai",
            confidence=0.9,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=1,
                symbol="BTC",
                side="short",
                lifecycle_status="exited",
                exit_reason="take_profit",
                signal_at=opened_at,
                entered_at=opened_at,
                exited_at=closed_at,
                entry_price_actual=66000,
                exit_price_actual=65000,
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get("/positions-panel")

    assert response.status_code == 200
    assert 'data-exchange-history-panel' in response.text
    assert 'data-deepcoin-history-position' not in response.text
    assert 'data-history-position-id="lifecycle:1"' not in response.text
    assert "暂无历史仓位" in response.text


def test_complete_history_metrics_keep_missing_actual_values_visible(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    opened_at = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    closed_at = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=1,
            posted_at=opened_at,
            sender_name="alice",
            text="BTC long Entry 66000 SL 65000 TP 68000",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="66000",
            stop_loss_text="65000",
            take_profit_text="68000",
            parse_source="text_ai",
            confidence=0.9,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=1,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="kol_signal",
                signal_at=opened_at,
                entered_at=opened_at,
                exited_at=closed_at,
                entry_price_actual=66000,
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get("/positions-panel")
    card = re.search(
        r'<article class="deepcoin-history-position" data-deepcoin-history-position '
        r'data-history-position-id="lifecycle:1">(.*?)</article>',
        response.text,
        re.DOTALL,
    )

    assert card is None


def test_history_position_handles_legacy_binding_without_metrics(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    closed_at = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                kol_id="legacy-binding",
                chat_id=100,
                message_id=1,
                symbol="BTC",
                side="short",
                venue="deepcoin",
                status="closed",
                created_at=closed_at,
                updated_at=closed_at,
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get("/positions-panel")
    card = re.search(
        r'<article class="deepcoin-history-position" data-deepcoin-history-position '
        r'data-history-position-id="binding:1">(.*?)</article>',
        response.text,
        re.DOTALL,
    )

    assert response.status_code == 200
    assert card is None


def test_binding_payload_backfill_restores_entry_price_and_contract_quantity(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    closed_at = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                kol_id="legacy-binding",
                chat_id=100,
                message_id=1,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                status="closed",
                payload_json=json.dumps(
                    {
                        "draft": {
                            "contract_spec": {"contract_value": 0.001},
                            "order_legs": [
                                {"price": 59100, "quantity": 7},
                                {"price": 58900, "quantity": 9},
                            ],
                        }
                    }
                ),
                created_at=closed_at,
                updated_at=closed_at,
            )
        )
        session.commit()

    row = list_exited_strategies(session_factory, limit=10)[0]
    response = TestClient(create_web_app(database_path=database_path)).get("/positions-panel")
    card = re.search(
        r'<article class="deepcoin-history-position" data-deepcoin-history-position '
        r'data-history-position-id="binding:1">(.*?)</article>',
        response.text,
        re.DOTALL,
    )

    assert row["entry_price_actual"] == 58987.5
    assert row["position_size_text"] == "0.016 BTC"
    assert row["history_metric_source"] == "saved_order_payload"
    assert response.status_code == 200
    assert card is None


def test_binding_payload_history_metrics_prefer_verified_deepcoin_values(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    closed_at = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                kol_id="legacy-binding",
                chat_id=100,
                message_id=1,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                status="closed",
                payload_json=json.dumps(
                    {
                        "draft": {
                            "contract_spec": {"contract_value": 0.001},
                            "order_legs": [{"price": 59100, "quantity": 7}],
                        },
                        "history_metrics": {
                            "avgPx": "58818.4",
                            "closeAvgPx": "57800",
                            "pnl": "-7.1288",
                            "pos": "7",
                            "closePos": "7",
                        },
                    }
                ),
                created_at=closed_at,
                updated_at=closed_at,
            )
        )
        session.commit()

    row = list_exited_strategies(session_factory, limit=10)[0]

    assert row["entry_price_actual"] == 58818.4
    assert row["exit_price_actual"] == 57800.0
    assert row["realized_pnl"] == -7.1288
    assert row["position_size_text"] == "0.007 BTC"
    assert row["history_metric_source"] == "deepcoin_position_history"


def test_verified_deepcoin_history_excludes_unfilled_and_keeps_complete_position(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        unfilled = ExecutionBinding(
            kol_id="unfilled",
            chat_id=100,
            message_id=1,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="closed",
            payload_json=json.dumps(
                {
                    "draft": {
                        "contract_spec": {"contract_value": 0.001},
                        "order_legs": [{"price": 60000, "quantity": 1}],
                    },
                    "history_metrics": {"avgPx": "60000"},
                }
            ),
        )
        verified = ExecutionBinding(
            kol_id="verified",
            chat_id=100,
            message_id=2,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="closed",
            payload_json=json.dumps(
                {
                    "draft": {
                        "contract_spec": {"contract_value": 0.001},
                        "order_legs": [{"price": 65000, "quantity": 1}],
                    },
                    "history_metrics": {
                        "avgPx": "65000",
                        "closeAvgPx": "64000",
                        "pnl": "11",
                        "pos": "2",
                        "closePos": "2",
                    }
                }
            ),
        )
        session.add_all([unfilled, verified])
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=verified.id,
                strategy_instance_id="verified-history",
                leg_index=0,
                purpose="entry",
                order_kind="market",
                pos_id="real-pos-1",
                venue="deepcoin",
                attribution_status="verified",
                status="closed",
            )
        )
        session.commit()

    rows = list_verified_deepcoin_history_positions(session_factory, limit=10)

    assert [row["execution_binding_id"] for row in rows] == [2]
    assert rows[0]["entry_price_actual"] == 65000.0
    assert rows[0]["exit_price_actual"] == 64000.0
    assert rows[0]["realized_pnl"] == 11.0
    assert rows[0]["position_size_text"] == "0.002 BTC"



def test_verified_deepcoin_history_orders_by_deepcoin_close_time(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        for message_id, pos_id, close_time in (
            (1, "position-early", "1784851200000"),
            (2, "position-late", "1784937600000"),
        ):
            binding = ExecutionBinding(
                kol_id=f"binding-{message_id}",
                chat_id=100,
                message_id=message_id,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                status="closed",
                payload_json=json.dumps(
                    {
                        "draft": {
                            "contract_spec": {"contract_value": 0.001},
                            "order_legs": [{"price": 65000, "quantity": 1}],
                        },
                        "history_metrics": {
                            "avgPx": "65000",
                            "closeAvgPx": "66000",
                            "pnl": "1",
                            "pos": "1",
                            "closePos": "1",
                            "uTime": close_time,
                        },
                    }
                ),
            )
            session.add(binding)
            session.flush()
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=f"history-{message_id}",
                    leg_index=0,
                    purpose="entry",
                    order_kind="market",
                    pos_id=pos_id,
                    venue="deepcoin",
                    attribution_status="verified",
                    status="closed",
                )
            )
        session.commit()

    rows = list_verified_deepcoin_history_positions(session_factory, limit=10)

    assert [row["message_id"] for row in rows] == [2, 1]


def test_history_position_time_tie_uses_source_independent_stable_identifier(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    shared_closed_at = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=1,
            posted_at=shared_closed_at,
            sender_name="alice",
            text="BTC long Entry 100 SL 90 TP 110",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="100",
            stop_loss_text="90",
            take_profit_text="110",
            parse_source="text_ai",
            confidence=0.9,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=1,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="take_profit",
                signal_at=shared_closed_at,
                entered_at=shared_closed_at,
                exited_at=shared_closed_at,
            )
        )
        session.add_all(
            [
                ExecutionBinding(
                    kol_id="binding-one",
                    chat_id=200,
                    message_id=1,
                    symbol="ETH",
                    side="long",
                    venue="deepcoin",
                    status="closed",
                    created_at=shared_closed_at,
                    updated_at=shared_closed_at,
                ),
                ExecutionBinding(
                    kol_id="binding-two",
                    chat_id=201,
                    message_id=1,
                    symbol="SOL",
                    side="short",
                    venue="deepcoin",
                    status="closed",
                    created_at=shared_closed_at,
                    updated_at=shared_closed_at,
                ),
            ]
        )
        session.commit()

    rows = list_exited_strategies(session_factory, limit=10)

    assert [row["history_sort_id"] for row in rows] == [
        "binding:2",
        "binding:1",
        "lifecycle:1",
    ]


def test_dashboard_renders_versioned_prompt_center_without_api_keys(tmp_path):
    config_path = tmp_path / "ai.yaml"
    config_path.write_text(
        """
mode: ai_provider
text_provider:
  base_url: https://example.test/v1
  api_key: never-render-this-secret
  model: deepseek-test
image_provider:
  base_url: https://example.test/v1
  api_key: never-render-this-image-secret
  model: mimo-test
""".strip(),
        encoding="utf-8",
    )
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )

    response = TestClient(app).get("/more-panel")

    assert response.status_code == 200
    assert "data-ai-prompt-center" in response.text
    assert "data-ai-prompt-list" in response.text
    assert "data-ai-prompt-mobile-select" in response.text
    assert "data-ai-prompt-detail" in response.text
    for control in (
        "data-ai-prompt-save-draft",
        "data-ai-prompt-validate",
        "data-ai-prompt-test",
        "data-ai-prompt-publish",
        "data-ai-prompt-history",
        "data-ai-prompt-rollback",
    ):
        assert control in response.text
    assert "DeepSeek = A + C" in response.text
    assert "MiMo = A + B + C" in response.text
    assert "never-render-this-secret" not in response.text
    assert "never-render-this-image-secret" not in response.text


def test_model_selection_page_exposes_independent_context_selector(tmp_path):
    config_path = tmp_path / "ai.yaml"
    config_path.write_text(
        "\n".join(
            [
                "active_text_model_id: deepseek-v4-flash",
                "active_image_model_id: mimo-v2.5",
                "context_resolution_model_id: mimo-v2.5",
            ]
        ),
        encoding="utf-8",
    )
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )

    response = TestClient(app).get("/more-panel")

    assert response.status_code == 200
    assert "data-context-resolution-model-id" in response.text
    assert re.search(r'<option value="mimo-v2\.5" selected>', response.text)


def test_root_shell_skips_group_strategy_and_configuration_loaders(tmp_path, monkeypatch):
    app = create_web_app(database_path=tmp_path / "research.db")

    def fail_heavy_loader(*_args, **_kwargs):
        raise AssertionError("root shell invoked a deferred loader")

    for loader_name in (
        "load_group_rows",
        "list_pending_strategies",
        "load_lifecycle_counts_by_chat_id",
        "list_holding_strategies",
        "load_trading_settings",
        "load_ai_recognition_config",
        "list_recognition_profiles",
    ):
        monkeypatch.setattr(
            f"telegram_kol_research.web_app.{loader_name}",
            fail_heavy_loader,
        )

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert 'data-lazy-workbench="groups"' in response.text
    assert 'data-lazy-workbench="more"' in response.text
    assert "data-group-link" not in response.text
    assert "data-ai-recognition-config" not in response.text
    assert "data-trading-settings-form" not in response.text
    assert "data-ai-model-api-key" not in response.text


def test_groups_workbench_shell_keeps_message_detail_host_on_root(tmp_path):
    response = TestClient(create_web_app(database_path=tmp_path / "research.db")).get("/")

    assert response.status_code == 200
    groups_start = response.text.index('data-workbench-panel="groups"')
    activity_start = response.text.index('data-workbench-panel="activity"')
    groups_panel = response.text[groups_start:activity_start]
    assert "data-detail-panel" in groups_panel
    assert 'data-mobile-work-region="messages"' not in groups_panel
    assert "选择群组后加载消息列表" in groups_panel


def test_groups_and_more_partials_retain_deferred_controls(tmp_path):
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            group_config=GroupConfig(
                groups=[TargetGroupConfig(chat_title="77", chat_id=77)]
            ),
        )
    )

    groups = client.get("/groups")
    more = client.get("/more-panel")

    assert groups.status_code == 200
    assert "kol-strategy-list" in groups.text
    assert "data-toggle-group-automation" in groups.text
    assert more.status_code == 200
    assert "data-ai-prompt-center" in more.text
    assert "data-ai-recognition-config" in more.text
    assert "data-trading-settings-form" in more.text
from telegram_kol_research.web_app import _exchange_order_row


def test_logs_page_has_safe_paginated_viewer_controls(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/logs")

    assert response.status_code == 200
    assert "data-log-viewer" in response.text
    assert "data-log-level-filter" in response.text
    assert "data-log-refresh" in response.text
    assert "data-log-list" in response.text


def test_index_page_renders_explicit_operational_states(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/home-dashboard")

    assert response.status_code == 200
    assert 'data-service-health="telegram"' in response.text
    assert 'data-service-health="database"' in response.text
    assert 'data-service-health="deepcoin"' in response.text
    assert "data-home-event-empty" in response.text
    assert "data-last-success-at" in response.text


def test_management_execution_mode_form_labels_shadow_and_live_risk(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/more-panel")

    assert response.status_code == 200
    assert 'name="management_execution_mode"' in response.text
    assert 'value="disabled"' in response.text
    assert 'value="shadow"' in response.text
    assert '影子：只生成计划，不写入交易所' in response.text
    assert 'value="live"' in response.text
    assert '实盘：高风险，可写入交易所' in response.text


def test_entry_preamble_rollout_controls_are_explicitly_dormant_by_default(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/more-panel")

    assert response.status_code == 200
    assert 'name="entry_preamble_mode"' in response.text
    assert 'name="entry_preamble_live_chat_ids"' not in response.text
    assert 'value="disabled" selected' in response.text
    assert '前置仓位指令组装模式' in response.text
    assert '测试：只记录，不改变真实下单' in response.text
    assert '实盘：所有已配置交易群组' in response.text


def test_adjacent_entry_rollout_controls_are_dormant_by_default(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/more-panel")

    assert response.status_code == 200
    assert 'name="entry_message_assembly_v2_mode"' in response.text
    assert 'name="entry_revision_v2_mode"' in response.text
    assert "相邻入场消息组装" in response.text
    assert "已提交入场修订" in response.text


def test_more_panel_labels_position_limit_as_per_group(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/more-panel")

    assert response.status_code == 200
    assert "每群组最大有效持仓数" in response.text
    assert 'name="max_concurrent_positions"' in response.text
    assert 'value="4"' in response.text


def test_index_page_is_a_lightweight_shell_without_deepcoin_or_message_timeline(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 7, 13, tzinfo=UTC),
                text="must be deferred until messages view opens",
            )
        )
        session.commit()

    deepcoin_factory_calls = []

    def tracking_deepcoin_factory():
        deepcoin_factory_calls.append(True)
        raise AssertionError("root page must not construct a Deepcoin client")

    client = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=tracking_deepcoin_factory,
        )
    )

    response = client.get("/")

    assert response.status_code == 200
    assert deepcoin_factory_calls == []
    assert 'data-lazy-workbench="strategies"' in response.text
    assert 'data-lazy-workbench="positions"' in response.text
    assert 'data-lazy-workbench="activity"' in response.text
    assert 'data-lazy-workbench="groups"' in response.text
    assert 'data-lazy-workbench="home"' not in response.text
    assert '/strategy-records?filter=needs_attention' in response.text
    assert "must be deferred until messages view opens" not in response.text
    assert "data-message-card" not in response.text
    assert "data-exchange-position-tabs" not in response.text


def test_deferred_home_and_positions_partials_render_independently(tmp_path):
    class EmptyDeepcoinClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=EmptyDeepcoinClient,
        )
    )

    home = client.get("/home-dashboard")
    positions = client.get("/positions-panel")

    assert home.status_code == 200
    assert "data-home-dashboard" in home.text
    assert "data-home-risk-summary" in home.text
    assert positions.status_code == 200
    assert "data-exchange-position-tabs" in positions.text
    assert 'data-exchange-position-tab="positions"' in positions.text


def test_positions_panel_builds_one_annotated_exchange_snapshot_per_request(tmp_path):
    factory_calls = []

    class EmptyDeepcoinClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    def tracking_factory():
        factory_calls.append(True)
        return EmptyDeepcoinClient()

    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=tracking_factory,
        )
    )

    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert factory_calls == [True]


class _RecordingExchangePositionsClient:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def list_positions(self):
        self.calls.append(("list_positions", None))
        return [
            {
                "pos": "1",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-live",
                "posSide": "long",
                "avgPx": "60000",
            }
        ]

    def list_open_orders(self):
        self.calls.append(("list_open_orders", None))
        return []

    def list_order_history(self):
        self.calls.append(("list_order_history", None))
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append(("list_trigger_orders_pending", inst_id))
        return []

    def list_trigger_order_history(self, *, inst_id):
        self.calls.append(("list_trigger_order_history", inst_id))
        return []

    def list_position_history(self, *, inst_id):
        self.calls.append(("list_position_history", inst_id))
        return []


def test_positions_panel_initial_load_reads_only_live_positions(tmp_path):
    exchange = _RecordingExchangePositionsClient()
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=lambda: exchange,
        )
    )

    response = client.get("/positions-panel?initial=positions")

    assert response.status_code == 200
    assert exchange.calls == [
        ("list_positions", None),
        ("list_trigger_orders_pending", "BTC-USDT-SWAP"),
    ]
    assert 'data-exchange-position-panel="open-orders"' in response.text
    assert 'data-exchange-tab-loaded="false"' in response.text


def _cached_live_position_snapshot(pos_id="cached-pos"):
    return {
        "positions": [
            {
                "symbol": "BTC",
                "inst_id": "BTC-USDT-SWAP",
                "pos_id": pos_id,
                "side": "long",
                "entry_text": "60000",
                "position_size_text": "1 contracts BTC-USDT-SWAP",
                "exchange_protection_orders": [],
            }
        ],
        "unattributed_protection_orders": [],
        "open_orders": [],
        "order_history": [],
        "position_history": [],
        "error": None,
    }


def test_positions_panel_fresh_snapshot_avoids_deepcoin_read(tmp_path):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    captured_at = datetime(2026, 7, 31, 8, 15, tzinfo=UTC)
    saved = LivePositionSnapshotStore(snapshot_path).finish_success(
        _cached_live_position_snapshot(),
        captured_at=captured_at,
    )
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        raise AssertionError("fresh position snapshot must avoid Deepcoin")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=forbidden_factory,
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 15, 2, tzinfo=UTC
        ),
    )

    response = TestClient(app).get("/positions-panel?initial=positions")

    assert response.status_code == 200
    assert factory_calls == []
    assert "cached-pos" in response.text
    assert f'data-position-snapshot-version="{saved.version}"' in response.text
    assert 'data-position-snapshot-state="current"' in response.text
    assert "持仓数据刚刚更新" in response.text


def test_split_web_position_loaders_never_construct_a_deepcoin_client(tmp_path):
    factory_calls = []
    app = create_web_app(
        database_path=tmp_path / "research.db",
        runtime_role="web",
        deepcoin_client_factory=lambda: factory_calls.append("called"),
        live_position_snapshot_path=tmp_path / "missing-snapshot.json",
    )

    with TestClient(app) as client:
        responses = [
            client.get("/positions-panel?initial=positions"),
            client.get("/positions-panel"),
            client.get("/positions-panel/tabs/open-orders"),
            client.get("/positions-panel/tabs/order-history"),
            client.get("/positions-panel/tabs/position-history"),
        ]

    assert [response.status_code for response in responses] == [200] * 5
    assert factory_calls == []
    assert all("Deepcoin 数据暂不可用" in response.text for response in responses)


def test_positions_view_server_renders_cached_snapshot_in_initial_document(tmp_path):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    LivePositionSnapshotStore(snapshot_path).finish_success(
        _cached_live_position_snapshot("initial-document-pos"),
        captured_at=datetime(2026, 7, 31, 8, 15, tzinfo=UTC),
    )
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("fresh initial document must avoid Deepcoin")
        ),
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 15, 2, tzinfo=UTC
        ),
    )

    response = TestClient(app).get("/?view=positions")

    assert response.status_code == 200
    assert "initial-document-pos" in response.text
    assert 'data-position-snapshot-state="current"' in response.text


def test_positions_view_server_renders_stale_snapshot_without_waiting_for_exchange(
    tmp_path,
):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    LivePositionSnapshotStore(snapshot_path).finish_success(
        _cached_live_position_snapshot("stale-initial-document-pos"),
        captured_at=datetime(2026, 7, 31, 8, 15, tzinfo=UTC),
    )
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("initial document must never wait for Deepcoin")
        ),
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 16, tzinfo=UTC
        ),
    )

    response = TestClient(app).get("/?view=positions")

    assert response.status_code == 200
    assert "stale-initial-document-pos" in response.text
    assert 'data-position-snapshot-state="stale"' in response.text


def test_positions_panel_stale_snapshot_returns_cached_then_refreshes(tmp_path):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    store = LivePositionSnapshotStore(snapshot_path)
    old = store.finish_success(
        _cached_live_position_snapshot(),
        captured_at=datetime(2026, 7, 31, 8, 15, tzinfo=UTC),
    )
    exchange = _RecordingExchangePositionsClient()
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: exchange,
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 15, 10, tzinfo=UTC
        ),
    )

    response = TestClient(app).get("/positions-panel?initial=positions")
    refreshed = app.state.live_position_snapshot_store.read()

    assert response.status_code == 200
    assert "cached-pos" in response.text
    assert 'data-position-snapshot-state="refreshing"' in response.text
    assert "正在刷新 Deepcoin" in response.text
    assert exchange.calls == [
        ("list_positions", None),
        ("list_trigger_orders_pending", "BTC-USDT-SWAP"),
    ]
    assert refreshed is not None
    assert refreshed.version != old.version
    assert refreshed.payload["positions"] == []
    assert (
        refreshed.payload["_live_source"]["positions"][0]["posId"]
        == "pos-live"
    )


def test_positions_panel_stale_snapshot_does_not_wait_for_background_refresh(
    tmp_path,
):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    LivePositionSnapshotStore(snapshot_path).finish_success(
        _cached_live_position_snapshot(),
        captured_at=datetime(2026, 7, 31, 8, 15, tzinfo=UTC),
    )
    started = threading.Event()
    release = threading.Event()

    class BlockingDeepcoinClient:
        def list_positions(self):
            started.set()
            assert release.wait(timeout=2)
            return []

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=BlockingDeepcoinClient,
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 15, 10, tzinfo=UTC
        ),
    )

    with TestClient(app) as client:
        started_at = time.monotonic()
        response = client.get("/positions-panel?initial=positions")
        elapsed = time.monotonic() - started_at
        assert started.wait(timeout=1)
        release.set()

    assert response.status_code == 200
    assert elapsed < 0.5
    assert "cached-pos" in response.text
    assert 'data-position-snapshot-state="refreshing"' in response.text


def test_positions_panel_without_snapshot_loads_and_persists_fallback(tmp_path):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    exchange = _RecordingExchangePositionsClient()
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: exchange,
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 15, tzinfo=UTC
        ),
    )

    response = TestClient(app).get("/positions-panel?initial=positions")
    persisted = LivePositionSnapshotStore(snapshot_path).read()

    assert response.status_code == 200
    assert "pos-live" not in response.text
    assert 'data-position-snapshot-state="refreshing"' in response.text
    assert persisted is not None
    assert persisted.payload["positions"] == []
    assert persisted.payload["_live_source"]["positions"][0]["posId"] == "pos-live"


def test_positions_panel_failed_refresh_preserves_successful_snapshot(tmp_path):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    store = LivePositionSnapshotStore(snapshot_path)
    saved = store.finish_success(
        _cached_live_position_snapshot(),
        captured_at=datetime(2026, 7, 31, 8, 15, tzinfo=UTC),
    )

    def broken_factory():
        raise RuntimeError("exchange unavailable")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=broken_factory,
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 16, tzinfo=UTC
        ),
    )

    response = TestClient(app).get("/positions-panel?initial=positions")
    current = app.state.live_position_snapshot_store.read()

    assert response.status_code == 200
    assert "cached-pos" in response.text
    assert current is not None
    assert current.version == saved.version
    assert current.last_error == "unavailable"


def test_positions_panel_rebuilds_local_evidence_instead_of_using_cached_attribution(
    tmp_path,
):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    LivePositionSnapshotStore(snapshot_path).finish_success(
        {
            **_cached_live_position_snapshot(),
            "_live_source": {
                "positions": [
                    {
                        "pos": "1",
                        "instId": "BTC-USDT-SWAP",
                        "posId": "cached-pos",
                        "posSide": "long",
                        "avgPx": "60000",
                    }
                ],
                "tpsl_orders": [],
                "tpsl_evidence_available": True,
            },
            "positions": [
                {
                    **_cached_live_position_snapshot()["positions"][0],
                    "persisted_attribution": {
                        "state": "bound",
                        "label": "已验证归属",
                        "group_name": "STALE-GROUP",
                    },
                }
            ],
        },
        captured_at=datetime(2026, 7, 31, 8, 15, tzinfo=UTC),
    )

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("cached response must not call Deepcoin")
        ),
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 15, 1, tzinfo=UTC
        ),
    )

    response = TestClient(app).get("/positions-panel?initial=positions")

    assert response.status_code == 200
    assert "cached-pos" in response.text
    assert "STALE-GROUP" not in response.text
    assert "归属待确认" in response.text


def test_position_snapshot_claim_is_not_leaked_when_rendering_fails(tmp_path):
    snapshot_path = tmp_path / "web-cache" / "positions.json"
    LivePositionSnapshotStore(snapshot_path).finish_success(
        {
            **_cached_live_position_snapshot(),
            "positions": None,
        },
        captured_at=datetime(2026, 7, 31, 8, 15, tzinfo=UTC),
    )
    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_position_snapshot_path=snapshot_path,
        position_snapshot_now_provider=lambda: datetime(
            2026, 7, 31, 8, 16, tzinfo=UTC
        ),
    )

    response = TestClient(app, raise_server_exceptions=False).get(
        "/positions-panel?initial=positions"
    )

    assert response.status_code == 500
    assert app.state.live_position_snapshot_store.begin_refresh() is True


@pytest.mark.parametrize(
    ("tab_name", "expected_methods"),
    [
        (
            "open-orders",
            {"list_open_orders", "list_trigger_orders_pending"},
        ),
        (
            "order-history",
            {"list_order_history", "list_trigger_order_history"},
        ),
        ("position-history", {"list_position_history"}),
    ],
)
def test_positions_panel_tab_route_reads_only_requested_dataset(
    tmp_path,
    tab_name,
    expected_methods,
):
    exchange = _RecordingExchangePositionsClient()
    captured_at = datetime(2026, 8, 10, 0, 5, 6, tzinfo=UTC)
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=lambda: exchange,
            now_provider=lambda: captured_at,
        )
    )

    response = client.get(f"/positions-panel/tabs/{tab_name}")

    assert response.status_code == 200
    assert {method for method, _inst_id in exchange.calls} == expected_methods
    assert f'data-exchange-position-panel="{tab_name}"' in response.text
    assert 'data-exchange-tab-loaded="true"' in response.text
    assert 'data-exchange-tab-item-count="0"' in response.text
    assert (
        'data-exchange-tab-captured-at="2026-08-10T00:05:06+00:00"'
        in response.text
    )


def test_position_history_tab_serves_continuation_from_stable_browse_snapshot(tmp_path):
    class ManyHistoryClient(_RecordingExchangePositionsClient):
        def list_position_history(self, *, inst_id):
            self.calls.append(("list_position_history", inst_id))
            if inst_id != "BTC-USDT-SWAP":
                return []
            return [
                {
                    "posId": f"pos-{index:03d}",
                    "instId": inst_id,
                    "posSide": "long",
                    "avgPx": "60000",
                    "closeAvgPx": "61000",
                    "closePos": "0.01",
                    "pnl": "10",
                    "uTime": str(1_700_000_000_000 - index),
                }
                for index in range(45)
            ]

    exchange = ManyHistoryClient()
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=lambda: exchange,
        )
    )

    first = client.get("/positions-panel/tabs/position-history")

    assert first.status_code == 200
    assert 'data-history-browse-token="' in first.text
    assert 'data-history-next-cursor="deepcoin-position:pos-019"' in first.text
    assert 'data-history-page-item-count="20"' in first.text
    assert 'data-history-total-count="45"' in first.text
    assert "data-history-visible-count" not in first.text
    assert 'data-history-has-more="true"' in first.text
    token = re.search(r'data-history-browse-token="([^"]+)"', first.text).group(1)

    second = client.get(
        "/positions-panel/tabs/position-history",
        params={"browse_token": token, "cursor": "deepcoin-position:pos-019"},
    )

    assert second.status_code == 200
    assert 'data-history-page-item-count="20"' in second.text
    assert 'data-history-total-count="45"' in second.text
    assert "pos-020" in second.text
    assert "pos-000" not in second.text
    assert len(exchange.calls) == 2


def test_position_history_tab_renders_browse_footer_and_filter_controls(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=_RecordingExchangePositionsClient,
    )
    client = TestClient(app)

    shell = client.get("/positions-panel?initial=positions")
    fragment = client.get("/positions-panel/tabs/position-history")

    assert 'data-history-position-filter' in shell.text
    assert 'data-history-filter-preset="30d"' in shell.text
    assert 'data-history-browse-footer' in fragment.text
    assert 'data-history-browse-status' in fragment.text


def test_position_history_tab_rejects_invalid_or_reversed_date_filter(tmp_path):
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=_RecordingExchangePositionsClient,
        )
    )

    invalid = client.get(
        "/positions-panel/tabs/position-history",
        params={"closed_after": "not-a-date"},
    )
    reversed_range = client.get(
        "/positions-panel/tabs/position-history",
        params={"closed_after": "2026-08-10", "closed_before": "2026-08-01"},
    )

    assert invalid.status_code == 422
    assert reversed_range.status_code == 422


def test_positions_panel_tab_failure_stays_retryable(tmp_path):
    class BrokenOpenOrdersClient(_RecordingExchangePositionsClient):
        def list_open_orders(self):
            self.calls.append(("list_open_orders", None))
            raise RuntimeError("exchange unavailable")

    exchange = BrokenOpenOrdersClient()
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=lambda: exchange,
        )
    )

    response = client.get("/positions-panel/tabs/open-orders")

    assert response.status_code == 200
    assert 'data-exchange-tab-loaded="false"' in response.text
    assert "Deepcoin 数据暂不可用" in response.text
    assert 'data-exchange-tab-retry="open-orders"' in response.text
    assert "重新加载" in response.text
    assert "data-exchange-tab-captured-at" not in response.text


def test_positions_panel_open_orders_does_not_drop_tpsl_after_twenty_regular_orders(
    tmp_path,
):
    class ManyOpenOrdersClient(_RecordingExchangePositionsClient):
        def list_open_orders(self):
            self.calls.append(("list_open_orders", None))
            return [
                {
                    "ordId": f"regular-{index}",
                    "instId": "BTC-USDT-SWAP",
                    "ordType": "limit",
                    "posSide": "long",
                }
                for index in range(25)
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            self.calls.append(("list_trigger_orders_pending", inst_id))
            return [
                {
                    "ordId": "tpsl-kept",
                    "instId": inst_id,
                    "triggerOrderType": "TPSL",
                    "posSide": "long",
                }
            ]

    exchange = ManyOpenOrdersClient()
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=lambda: exchange,
        )
    )

    response = client.get("/positions-panel/tabs/open-orders")

    assert response.status_code == 200
    assert "order tpsl-kept" in response.text


def test_position_history_tab_includes_persisted_history_symbols(tmp_path):
    exchange = _RecordingExchangePositionsClient()

    _load_exchange_tab_snapshot(
        create_session_factory(tmp_path / "research.db"),
        tab_name="position-history",
        deepcoin_client_factory=lambda: exchange,
        group_label_by_chat_id={},
        pending_entry_signals=[],
        trading_settings=SimpleNamespace(allowed_symbols=[]),
        known_history_symbols=["DOGE"],
    )

    assert exchange.calls == [
        ("list_position_history", "DOGE-USDT-SWAP"),
    ]


def _seed_live_bound_strategy(database_path, *, pos_id: str = "pos-live") -> None:
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        candidate = SignalCandidate(
            raw_message_id=70_001,
            symbol="BTCUSDT",
            side="long",
        )
        session.add(candidate)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=candidate.raw_message_id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="accepted",
                authoritative_payload_json="{}",
                agreement_status="agreed",
                differences_json="[]",
                prompt_versions_json="{}",
            )
        )
        binding = ExecutionBinding(
            strategy_instance_id=f"strategy:{pos_id}",
            kol_id="77",
            chat_id=77,
            message_id=701,
            symbol="BTCUSDT",
            side="long",
            venue="deepcoin",
            pos_id=pos_id,
            status="open",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=77,
                message_id=701,
                symbol="BTCUSDT",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 16, tzinfo=UTC),
                entered_at=datetime(2026, 7, 16, tzinfo=UTC),
                stop_loss=60_000,
                execution_binding_id=binding.id,
            )
        )
        session.commit()


def test_strategy_records_api_keeps_exchange_attention_and_builds_one_snapshot(tmp_path):
    database_path = tmp_path / "research.db"
    _seed_live_bound_strategy(database_path, pos_id="pos-missing")
    factory_calls = []

    class EmptyDeepcoinClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    def tracking_factory():
        factory_calls.append(True)
        return EmptyDeepcoinClient()

    response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=tracking_factory,
        )
    ).get("/api/strategy-records")

    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["filter"] == "needs_attention"
    assert payload["scan_scope"]["recent_limit"] >= 200
    assert payload["summary_counts"]["all"] == 1
    assert payload["summary_counts"]["needs_attention"] == 1
    assert factory_calls == [True]
    assert len(payload["records"]) == 1
    assert payload["records"][0]["attention"]["code"] == "position_missing"
    assert payload["records"][0]["exchange_state"] == "attention"


def test_strategy_records_api_never_crowds_out_old_missing_live_binding(tmp_path):
    database_path = tmp_path / "research.db"
    _seed_live_bound_strategy(database_path, pos_id="old-missing-pos")
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=88,
                    message_id=10_000 + index,
                    symbol="ETHUSDT",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 7, 16, tzinfo=UTC),
                    stop_loss=2_800,
                    updated_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
                )
                for index in range(1_001)
            ]
        )
        session.commit()

    class EmptyDeepcoinClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=EmptyDeepcoinClient,
        )
    ).get("/api/strategy-records")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scan_scope"]["current_live_binding_scope"] == "all"
    old_record = next(
        row for row in payload["records"] if row["pos_id"] == "old-missing-pos"
    )
    assert old_record["attention"]["code"] == "position_missing"
    assert payload["summary_counts"]["all"] == 1_002
    assert payload["summary_counts"]["pending_entry"] == 1_001
    assert payload["summary_counts"]["needs_attention"] == 1


def test_strategy_records_api_returns_exchange_orphan_evidence(tmp_path):
    class OrphanDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "orphan-pos",
                    "posSide": "long",
                    "pos": "1.5",
                    "avgPx": "3000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=OrphanDeepcoinClient,
        )
    ).get("/api/strategy-records")

    assert response.status_code == 200
    record = response.json()["records"][0]
    assert record["lifecycle_id"] is None
    assert record["pos_id"] == "orphan-pos"
    assert record["attention"]["code"] == "unattributed_position"
    assert record["detail_href"] == "/?view=positions&pos_id=orphan-pos"
    assert response.json()["summary_counts"]["all"] == 1
    assert response.json()["summary_counts"]["needs_attention"] == 1


def test_strategy_records_api_preserves_exchange_error_as_unknown(tmp_path):
    database_path = tmp_path / "research.db"
    _seed_live_bound_strategy(database_path)

    class BrokenDeepcoinClient:
        def list_positions(self):
            raise RuntimeError("exchange unavailable")

    response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=BrokenDeepcoinClient,
        )
    ).get("/api/strategy-records")

    assert response.status_code == 200
    payload = response.json()
    record = payload["records"][0]
    assert record["exchange_state"] == "unknown"
    assert record["real_position"] is None
    assert record["attention"]["code"] == "exchange_unavailable"
    assert payload["summary_counts"]["all"] == 1
    assert payload["summary_counts"]["needs_attention"] == 1


def test_strategy_records_api_exposes_safe_exchange_error_with_empty_database(tmp_path):
    class BrokenDeepcoinClient:
        def list_positions(self):
            raise RuntimeError("secret-api-key=do-not-render")

    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=BrokenDeepcoinClient,
        )
    ).get("/api/strategy-records")

    assert response.status_code == 200
    payload = response.json()
    assert payload["records"] == []
    assert payload["exchange_state"] == "unknown"
    assert payload["exchange_error"] is True
    assert payload["exchange_message"] == "Deepcoin 仓位快照暂不可用"
    assert "do-not-render" not in response.text


def test_strategy_records_api_includes_orphan_live_binding_without_duplicate_exchange_orphan(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                strategy_instance_id="orphan-live-binding",
                kol_id="77",
                chat_id=77,
                message_id=770,
                symbol="ETHUSDT",
                side="long",
                venue="deepcoin",
                pos_id="same-orphan-pos",
                status="open",
            )
        )
        session.commit()

    class OrphanPositionClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "same-orphan-pos",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "3000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=OrphanPositionClient,
        )
    ).get("/api/strategy-records")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scan_scope"]["orphan_live_binding_scope"] == "all"
    assert len(payload["records"]) == 1
    record = payload["records"][0]
    assert record["lifecycle_id"] is None
    assert record["orphan_execution_binding"] is True
    assert record["pos_id"] == "same-orphan-pos"
    assert record["real_position"]["pos_id"] == "same-orphan-pos"
    assert record["attention"]["code"] == "binding_without_lifecycle"


def test_strategy_records_api_never_reconciles_other_venue_pos_id_with_deepcoin(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        lifecycle_binding = ExecutionBinding(
            strategy_instance_id="gate-with-lifecycle",
            kol_id="77",
            chat_id=77,
            message_id=771,
            symbol="BTCUSDT",
            side="long",
            venue="gate",
            pos_id="reused-lifecycle-pos",
            status="open",
        )
        orphan_binding = ExecutionBinding(
            strategy_instance_id="gate-without-lifecycle",
            kol_id="88",
            chat_id=88,
            message_id=881,
            symbol="SOLUSDT",
            side="long",
            venue="other-exchange",
            pos_id="reused-orphan-pos",
            status="active",
        )
        session.add_all([lifecycle_binding, orphan_binding])
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=77,
                message_id=771,
                symbol="BTCUSDT",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 16, tzinfo=UTC),
                entered_at=datetime(2026, 7, 16, tzinfo=UTC),
                stop_loss=60_000,
                execution_binding_id=lifecycle_binding.id,
            )
        )
        session.commit()

    class DeepcoinCollisionClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "reused-lifecycle-pos",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "62000",
                },
                {
                    "instId": "SOL-USDT-SWAP",
                    "posId": "reused-orphan-pos",
                    "posSide": "long",
                    "pos": "2",
                    "avgPx": "150",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=DeepcoinCollisionClient,
        )
    ).get("/api/strategy-records?filter_name=all")

    assert response.status_code == 200
    records = response.json()["records"]
    assert {row["pos_id"] for row in records} == {
        "reused-lifecycle-pos",
        "reused-orphan-pos",
    }
    gate_record = next(row for row in records if row["venue"] == "gate")
    assert gate_record["exchange_state"] == "not_applicable"
    assert gate_record["real_position"] is None
    deepcoin_records = [row for row in records if row["venue"] == "deepcoin"]
    assert {row["pos_id"] for row in deepcoin_records} == {
        "reused-lifecycle-pos",
        "reused-orphan-pos",
    }
    assert all(row["lifecycle_id"] is None for row in deepcoin_records)
    assert all(not row.get("orphan_execution_binding") for row in deepcoin_records)
    assert all(
        row["attention"]["code"] == "unattributed_position"
        for row in deepcoin_records
    )
    assert response.json()["summary_counts"]["all"] == 3
    assert response.json()["summary_counts"]["needs_attention"] == 2


def test_deferred_home_marks_deepcoin_error_without_blocking_the_shell(tmp_path):
    class BrokenDeepcoinClient:
        def list_positions(self):
            raise RuntimeError("exchange unavailable")

    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=BrokenDeepcoinClient,
        )
    )

    root = client.get("/")
    home = client.get("/home-dashboard")

    assert root.status_code == 200
    assert home.status_code == 200
    assert "Deepcoin · error" in home.text


def test_shared_group_context_renders_all_groups(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=77, message_id=1, posted_at=datetime(2026, 7, 12, tzinfo=UTC), sender_name="Andy", text="one"),
                RawMessage(chat_id=88, message_id=1, posted_at=datetime(2026, 7, 12, 1, tzinfo=UTC), sender_name="币姐", text="two"),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups")

    assert response.status_code == 200
    assert response.text.count("data-group-link") == 2
    assert 'data-chat-id="77"' in response.text
    assert 'data-chat-id="88"' in response.text
    assert "data-message-group-select" not in response.text


def test_index_page_shows_group_list_and_messages(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                text="hello web",
            )
        )
        session.commit()

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="77",
                        chat_id=77,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            live_listener_status_reason="缂哄皯 Telegram API 鍑嵁",
            now_provider=lambda: datetime(2026, 4, 21, tzinfo=UTC),
        )
    )
    root_response = client.get("/")
    response = SimpleNamespace(
        status_code=root_response.status_code,
        text="\n".join(
            [
                root_response.text,
                client.get("/groups").text,
                client.get("/more-panel").text,
                client.get("/home-dashboard").text,
                client.get("/positions-panel").text,
                client.get("/groups/77/strategy-mid-panel?filter=holding").text,
                client.get("/groups/77/detail").text,
            ]
        ),
    )

    assert response.status_code == 200
    assert "data-trader-dashboard" in response.text
    assert 'data-workbench-view="strategies"' in response.text
    assert 'data-workbench-view="positions"' in response.text
    assert "data-home-risk-summary" in response.text
    assert "data-home-event-feed" in response.text
    assert 'data-home-event-filter="risk"' in response.text
    assert "data-desktop-workbench-nav" in response.text
    assert "data-mobile-work-nav" in response.text
    assert 'data-mobile-work-view="strategies"' in response.text
    assert 'data-mobile-work-view="positions"' in response.text
    assert 'data-mobile-work-view="activity"' in response.text
    assert 'data-mobile-work-view="groups"' in response.text
    assert 'data-mobile-work-view="more"' in response.text
    assert 'data-mobile-work-region="groups"' in response.text
    assert "data-dashboard-tab" in response.text
    assert "data-ai-prompt-center" in response.text
    assert "data-ai-recognition-config" in response.text
    assert "data-dashboard-tab" in response.text
    assert "data-ai-recognition-prompt" not in response.text
    assert "data-ai-recognition-config" in response.text
    assert "data-ai-model-selection" in response.text
    assert "data-context-resolution-model-id" in response.text
    assert "data-trading-settings-form" in response.text
    assert 'data-dashboard-tab="exchange-positions"' in response.text
    assert 'data-dashboard-panel="exchange-positions"' in response.text
    assert "交易持仓" in response.text
    assert 'data-exchange-position-tabs' in response.text
    assert 'data-exchange-position-tab="positions"' in response.text
    assert 'data-exchange-position-tab="open-orders"' in response.text
    assert 'data-exchange-position-tab="order-history"' in response.text
    assert 'data-exchange-position-tab="position-history"' in response.text
    assert 'data-exchange-position-label="当前委托"' in response.text
    assert 'data-exchange-position-label="历史委托"' in response.text
    assert 'data-exchange-position-label="历史仓位"' in response.text
    assert "data-exchange-tab-refresh-controls" in response.text
    assert "data-exchange-tab-refresh" in response.text
    assert "data-exchange-tab-refresh-status" in response.text
    exchange_tabs = re.search(
        r'<div class="exchange-tab-strip".*?</div>', response.text, re.S
    )
    assert exchange_tabs is not None
    assert exchange_tabs.group(0).index("持仓") < exchange_tabs.group(0).index("当前委托")
    assert exchange_tabs.group(0).index("当前委托") < exchange_tabs.group(0).index(
        "历史委托"
    )
    assert exchange_tabs.group(0).index("历史委托") < exchange_tabs.group(0).index(
        "历史仓位"
    )
    assert 'data-dashboard-tab="recognition-profiles"' in response.text
    assert 'data-dashboard-panel="recognition-profiles"' in response.text
    assert "比特币军长-11分组" in response.text
    assert "junzhang_profile" in response.text
    assert "止损上移到开仓价" in response.text
    assert "默认单笔最大亏损 USDT" in response.text
    assert "data-symbol-selector" in response.text
    assert "data-symbol-search" in response.text
    assert "data-selected-symbol-list" in response.text
    assert "data-selected-symbol-risk-list" in response.text
    assert 'name="symbol_max_loss_usdt"' in response.text
    assert 'name="symbol_entry_thresholds"' in response.text
    assert "data-symbol-entry-thresholds-input" in response.text
    assert 'type="hidden" name="max_market_entry_deviation_pct"' in response.text
    assert 'type="hidden" name="entry_range_order_style"' in response.text
    assert "现价入场最大偏离 %" not in response.text
    assert "区间入场方式" not in response.text
    assert "单点“附近”市价容忍 %" in response.text
    assert "20.0" in response.text
    assert "DeepSeek V4 Flash" in response.text
    assert "MiMo V2.5" in response.text
    assert "mimo-v2.5" in response.text
    assert "data-ai-model-row" in response.text
    assert "data-active-text-model-id" in response.text
    assert "data-active-image-model-id" in response.text
    assert 'data-strategy-filter="holding"' in response.text
    assert 'data-strategy-filter="pending"' in response.text
    assert 'data-strategy-filter="exited"' in response.text
    assert 'data-strategy-workflow-filter="executing"' in response.text
    assert 'data-strategy-workflow-filter="confirmation"' in response.text
    assert 'data-strategy-workflow-filter="abnormal"' in response.text
    assert 'data-message-workflow-filter="recognized"' in response.text
    assert "data-group-link" in response.text
    assert "data-trader-dashboard" in response.text
    assert "data-detail-panel" in response.text
    assert "77" in response.text
    assert "data-group-link" in response.text
    assert 'data-setting="ai_strategy_enabled"' in response.text
    assert 'data-setting="auto_trade_enabled"' in response.text
    assert "data-toggle-group-automation" in response.text
    assert 'data-setting="ai_strategy_enabled"' in response.text
    assert 'data-setting="auto_trade_enabled"' in response.text
    assert "is-enabled" in response.text
    assert "data-run-recovery-scan" in response.text
    assert "data-recovery-status" in response.text
    assert "data-layout-scroll-panel" in response.text
    assert "Conversation" not in response.text
    assert "data-ai-report-feed" not in response.text
    assert "data-group-prompt-panel" not in response.text
    assert "缇ょ粍鍒嗘瀽鍋忓ソ" not in response.text
    assert "data-group-prompt-panel" not in response.text
    assert "data-ai-workbench" not in response.text
    assert "data-ai-report-feed" not in response.text
    assert "data-ai-history-scroll" not in response.text
    assert "data-ai-composer" not in response.text
    assert "data-clear-ai-history" not in response.text
    assert 'textarea name="question"' not in response.text
    assert "Scope" not in response.text
    assert "Posted after" not in response.text
    assert "data-message-select" not in response.text
    assert "data-message-select" not in response.text
    assert "data-ai-output" not in response.text
    assert "data-ai-sources" not in response.text
    assert "source-preview" not in response.text

    # Message details lazy-loaded 鈥?test via tab endpoint
    msgs_resp = client.get("/groups/77/detail/tab/messages")
    assert msgs_resp.status_code == 200
    assert "hello web" in msgs_resp.text
    assert "2026-04-02 08:00" in msgs_resp.text
    assert "message-sync-state" in msgs_resp.text
    assert "Telegram API" in msgs_resp.text
    assert "2026-04-02 08:00" in msgs_resp.text
    assert "456.0" in msgs_resp.text
    assert "data-message-sticky-header" in msgs_resp.text
    assert "data-message-filter-panel" in msgs_resp.text
    assert "<summary>" in msgs_resp.text


def test_mobile_navigation_has_exactly_five_primary_destinations(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/")

    assert response.status_code == 200
    desktop_nav = re.search(
        r'<nav class="desktop-workbench-nav".*?</nav>', response.text, re.S
    )
    mobile_nav = re.search(
        r'<nav class="mobile-work-nav".*?</nav>', response.text, re.S
    )
    assert desktop_nav is not None
    assert mobile_nav is not None
    for view, label in (
        ("strategies", "策略"),
        ("positions", "持仓"),
        ("activity", "动态"),
        ("groups", "群组"),
        ("more", "更多"),
    ):
        assert f'data-workbench-view="{view}"' in mobile_nav.group(0)
        assert label in mobile_nav.group(0)
    assert mobile_nav.group(0).count("data-workbench-view=") == 5
    assert 'data-workbench-view="home"' not in mobile_nav.group(0)
    assert 'data-workbench-view="messages"' not in mobile_nav.group(0)
    assert 'data-workbench-view="management-batches"' not in mobile_nav.group(0)
    assert re.search(
        r'data-workbench-view="strategies"[^>]*aria-current="page"',
        mobile_nav.group(0),
    )
    assert 'data-workbench-view="management-batches"' not in desktop_nav.group(0)
    more_panel = re.search(
        r'<section class="workbench-panel more-workbench-panel".*?</section>',
        response.text,
        re.S,
    )
    assert more_panel is not None
    assert 'data-legacy-workbench-view="management-batches"' in more_panel.group(0)


def test_bound_position_close_is_not_rendered_for_unbound_exchange_position(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                text="hello web",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "posId": "pos-live-1",
                    "posSide": "long",
                    "pos": "0.01",
                    "avgPx": "63200",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "limit-1",
                    "side": "buy",
                    "ordType": "limit",
                    "state": "live",
                    "px": "63100",
                    "sz": "10",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "hist-1",
                    "side": "sell",
                    "ordType": "market",
                    "state": "filled",
                    "px": "63300",
                    "sz": "10",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "ordId": "trigger-1",
                    "posSide": "long",
                    "side": "sell",
                    "triggerOrderType": "TPSL",
                    "state": "live",
                    "triggerPrice": "62000",
                    "sz": "10",
                }
            ]

        def list_trigger_order_history(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "ordId": "trigger-hist-1",
                    "side": "sell",
                    "triggerOrderType": "TPSL",
                    "state": "cancelled",
                    "triggerPrice": "62000",
                    "sz": "10",
                }
            ]

    client = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "持仓(1)" in response.text
    assert "当前委托(2)" in response.text
    assert "历史委托(2)" in response.text
    assert "pos pos-live-1" in response.text
    assert "order limit-1" in response.text
    assert "order trigger-1" in response.text
    assert "order hist-1" in response.text
    assert "order trigger-hist-1" in response.text
    assert "止盈止损/平多" in response.text
    assert "data-close-bound-position" not in response.text


def test_positions_panel_renders_all_exchange_protection_orders(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "5",
                    "avgPx": "64000",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return [
                {
                    "ordId": "combined-1",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "sz": "3",
                    "tpTriggerPx": "66000",
                    "slTriggerPx": "62000",
                },
                {
                    "ordId": "legacy-stop-1",
                    "triggerOrderType": "TPSL",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "sz": "0",
                    "slTriggerPx": "61000",
                },
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id=None):
            return []

    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=FakeDeepcoinClient,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    assert "交易所保护单" in response.text
    assert "66000" in response.text
    assert "62000" in response.text
    assert "61000" in response.text
    assert "已验证归属" in response.text
    assert "无法归属" in response.text
    assert "order combined-1" in response.text
    assert "order legacy-stop-1" in response.text


def test_positions_panel_renders_full_position_tpsl_quantity_semantics(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-full-1",
                    "posSide": "long",
                    "pos": "10",
                    "avgPx": "63895.725",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return [
                {
                    "ordId": "tp-full-1",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-full-1",
                    "instId": "BTC-USDT-SWAP",
                    "sz": "0",
                    "tpTriggerPx": "66330",
                },
                {
                    "ordId": "tp-partial-1",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-full-1",
                    "instId": "BTC-USDT-SWAP",
                    "sz": "2",
                    "tpTriggerPx": "67000",
                },
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id=None):
            return []

    contract_specs = StaticDeepcoinContractSpecProvider(
        specs_by_instrument_id={
            "BTC-USDT-SWAP": DeepcoinContractSpec(
                instrument_id="BTC-USDT-SWAP",
                contract_value=0.001,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.1,
            )
        }
    )
    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=FakeDeepcoinClient,
            deepcoin_contract_spec_provider=contract_specs,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    assert "数量 全部剩余仓位（当前 10 contracts / 0.01 BTC）" in response.text
    assert "数量 2 contracts / 0.002 BTC" in response.text
    assert "数量 0 contracts" not in response.text


def test_positions_panel_renders_unattributed_full_position_tpsl_without_snapshot(
    tmp_path,
):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-a",
                    "posSide": "long",
                    "pos": "10",
                    "avgPx": "63895.725",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-b",
                    "posSide": "long",
                    "pos": "20",
                    "avgPx": "64000",
                },
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return [
                {
                    "ordId": "unknown-full-stop",
                    "triggerOrderType": "TPSL",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "sz": "0",
                    "slTriggerPx": "61000",
                }
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id=None):
            return []

    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=FakeDeepcoinClient,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    assert "全部仓位（具体仓位未归属）" in response.text
    assert "全部剩余仓位（当前 10 contracts）" not in response.text
    assert "全部剩余仓位（当前 20 contracts）" not in response.text


def test_positions_panel_summary_uses_ordered_verified_exchange_protection(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-summary-1",
                    "posSide": "long",
                    "pos": "3",
                    "avgPx": "63894.1",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return [
                {
                    "ordId": "tp-high",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-summary-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "tpTriggerPx": "70300",
                },
                {
                    "ordId": "stop-secondary",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-summary-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "slTriggerPx": "60878",
                },
                {
                    "ordId": "tp-low",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-summary-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "tpTriggerPx": "67100",
                },
                {
                    "ordId": "stop-primary",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-summary-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "slTriggerPx": "61000",
                },
                {
                    "ordId": "tp-middle",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-summary-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "tpTriggerPx": "68500",
                },
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id=None):
            return []

    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=FakeDeepcoinClient,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    assert _summarize_verified_exchange_protection_rows(
        [
            {"kind": "take_profit", "trigger_price_text": "70300", "ownership_state": "已验证归属"},
            {"kind": "stop_loss", "trigger_price_text": "60878", "ownership_state": "已验证归属"},
            {"kind": "take_profit", "trigger_price_text": "67100", "ownership_state": "已验证归属"},
            {"kind": "stop_loss", "trigger_price_text": "61000", "ownership_state": "已验证归属"},
            {"kind": "take_profit", "trigger_price_text": "68500", "ownership_state": "已验证归属"},
        ],
        side="long",
    ) == ("61000", "60878", ("67100", "68500", "70300"))
    assert "止盈止损(5)" in response.text


def test_positions_panel_keeps_tpsl_out_of_compact_summary(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-compact-1",
                    "posSide": "long",
                    "pos": "3",
                    "avgPx": "63894.1",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return [
                {
                    "ordId": "compact-stop",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-compact-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "slTriggerPx": "61000",
                },
                {
                    "ordId": "compact-tp",
                    "triggerOrderType": "TPSL",
                    "posId": "pos-compact-1",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "tpTriggerPx": "67200",
                },
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id=None):
            return []

    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=FakeDeepcoinClient,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    summary_markup = response.text.split('<section class="exchange-protection-orders"', 1)[0]
    assert "<dt>止损</dt>" not in summary_markup
    assert "<dt>第二止损</dt>" not in summary_markup
    assert "<dt>止盈</dt>" not in summary_markup
    assert "止盈止损(2)" in response.text
    assert "67200" in response.text
    assert "61000" in response.text


def test_positions_panel_keeps_available_protection_when_another_instrument_fails(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "btc-pos",
                    "posSide": "long",
                    "pos": "5",
                    "avgPx": "64000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "eth-pos",
                    "posSide": "long",
                    "pos": "2",
                    "avgPx": "2000",
                },
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            if inst_id == "ETH-USDT-SWAP":
                raise RuntimeError("ETH pending TPSL unavailable")
            return [
                {
                    "ordId": "btc-stop-1",
                    "triggerOrderType": "TPSL",
                    "posId": "btc-pos",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "sz": "5",
                    "slTriggerPx": "62000",
                }
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id=None):
            return []

    response = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            deepcoin_client_factory=FakeDeepcoinClient,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    assert "order btc-stop-1" in response.text
    assert "62000" in response.text


def test_positions_panel_lists_unattributed_protection_once_outside_position_cards(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {"instId": "BTC-USDT-SWAP", "posId": "pos-a", "posSide": "long", "pos": "3", "avgPx": "64000"},
                {"instId": "BTC-USDT-SWAP", "posId": "pos-b", "posSide": "long", "pos": "4", "avgPx": "64100"},
            ]

        def list_open_orders(self, *, inst_id=None): return []
        def list_order_history(self, *, inst_id=None): return []
        def list_trigger_order_history(self, *, inst_id=None): return []
        def list_position_history(self, *, inst_id=None): return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return [
                {"ordId": "direct-a", "triggerOrderType": "TPSL", "instId": inst_id, "posId": "pos-a", "posSide": "long", "sz": "3", "slTriggerPx": "62000"},
                {"ordId": "legacy-1", "triggerOrderType": "TPSL", "instId": inst_id, "side": "sell", "sz": "0", "slTriggerPx": "61000"},
            ]

    response = TestClient(create_web_app(
        database_path=tmp_path / "research.db", deepcoin_client_factory=FakeDeepcoinClient,
    )).get("/positions-panel")

    assert response.status_code == 200
    cards = {
        pos_id: re.search(
            rf'<article class="exchange-position-card" data-position-pos-id="{pos_id}".*?</article>',
            response.text,
            re.DOTALL,
        ).group(0)
        for pos_id in ("pos-a", "pos-b")
    }
    assert "order direct-a" in cards["pos-a"]
    assert "order legacy-1" not in cards["pos-a"]
    assert "order legacy-1" not in cards["pos-b"]
    summary = re.search(
        r'<section[^>]*data-unattributed-protection-orders[^>]*>.*?</section>',
        response.text,
        re.DOTALL,
    ).group(0)
    assert "未归属交易所保护单" in summary
    assert "order legacy-1" in summary
    assert response.text.index("data-unattributed-protection-orders") > response.text.index(
        'data-exchange-view-panel="grouped"'
    )


def test_ledger_order_position_fallback_renders_tpsl_on_exact_position(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:100", chat_id=100, message_id=1, symbol="BTC",
            side="long", venue="deepcoin", status="active", pos_id="pos-a",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id, leg_index=1, purpose="entry",
            order_kind="market", pos_id="pos-a", venue="deepcoin",
            attribution_status="verified", status="active",
            response_json=json.dumps({"posId": "pos-a"}),
            attribution_evidence_json=json.dumps({"evidence_type": "exact_regular_order_id"}),
        )
        session.add(leg)
        session.flush()
        session.add(PositionProtectionLedger(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id, pos_id="pos-a",
            instrument_id="BTC-USDT-SWAP", side="long", order_id="ledger-tp-1",
            purpose="take_profit", trigger_price="66000", status="verified",
            evidence_source="native_tpsl_readback",
        ))
        session.add(PositionTakeProfitOrder(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id, pos_id="pos-a",
            order_id="recorded-tp-1", trigger_price="66500", status="active",
        ))
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [{"instId": "BTC-USDT-SWAP", "posId": "pos-a", "posSide": "long", "pos": "3", "avgPx": "64000"}]

        def list_open_orders(self, *, inst_id=None): return []
        def list_order_history(self, *, inst_id=None): return []
        def list_trigger_order_history(self, *, inst_id=None): return []
        def list_position_history(self, *, inst_id=None): return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return [
                {"ordId": "ledger-tp-1", "triggerOrderType": "TPSL", "instId": inst_id, "side": "sell", "sz": "3", "tpTriggerPx": "66000"},
                {"ordId": "recorded-tp-1", "triggerOrderType": "TPSL", "instId": inst_id, "side": "sell", "sz": "1", "tpTriggerPx": "66500"},
                {"ordId": "unknown-tp-1", "triggerOrderType": "TPSL", "instId": inst_id, "side": "sell", "sz": "0", "tpTriggerPx": "67000"},
            ]

    response = TestClient(create_web_app(
        database_path=database_path, deepcoin_client_factory=FakeDeepcoinClient,
    )).get("/positions-panel")

    card = re.search(
        r'<article class="exchange-position-card" data-position-pos-id="pos-a".*?</article>',
        response.text,
        re.DOTALL,
    ).group(0)
    summary = re.search(
        r'<section[^>]*data-unattributed-protection-orders[^>]*>.*?</section>',
        response.text,
        re.DOTALL,
    ).group(0)
    assert "order ledger-tp-1" in card
    assert "order ledger-tp-1" not in summary
    assert "order recorded-tp-1" not in card
    assert "order recorded-tp-1" in summary
    assert re.search(
        r"order ledger-tp-1</code>\s*<span class=\"exchange-attribution-chip\">已验证归属</span>",
        card,
    )
    assert "order unknown-tp-1" in summary


def test_bound_position_close_renders_exact_context_for_bound_exchange_position(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=56,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400",
            stop_loss_text="60800",
            take_profit_text="63600",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:56:BTC:long",
            kol_id="group:100",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-56",
            pos_id="pos-live-1",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="market",
                order_id="order-56",
                pos_id="pos-live-1",
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json=json.dumps(
                    {"evidence_type": "exact_regular_order_id"}
                ),
                last_verified_at=datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
                status="active",
            )
        )
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=56,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62400,
                entry_price_actual=62400,
                stop_loss=60800,
                take_profit="63600",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "posId": "pos-live-1",
                    "posSide": "long",
                    "pos": "0.01",
                    "avgPx": "62400",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Alpha Group",
                        chat_id=100,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert 'data-exchange-view-mode="list"' in response.text
    assert 'data-exchange-view-mode="grouped"' in response.text
    assert "已验证归属" in response.text
    assert "Alpha Group" in response.text
    assert "BTC long" in response.text
    assert "deepcoin:100:56:BTC:long" in response.text
    assert "entry leg #2" in response.text
    assert "exact_regular_order_id" in response.text
    assert "pos-live-1" in response.text
    assert "2026-07-14 16:00:00" in response.text
    assert 'data-exchange-group-section' in response.text
    assert 'data-position-danger-zone' in response.text
    assert 'href="/strategy-records/1"' in response.text
    assert 'data-strategy-record-link="1"' in response.text
    close_buttons = re.findall(
        r'<button[^>]+data-close-bound-position[^>]*>', response.text,
    )
    assert close_buttons
    for button in close_buttons:
        assert 'data-pos-id="pos-live-1"' in button
        assert 'data-live-action-symbol="BTC"' in button
        assert 'data-live-action-side="long"' in button
        assert 'data-live-action-size="0.01 contracts BTCUSDT"' in button
        assert 'data-live-action-label="市价全平"' in button
        assert 'data-live-action-confirmation-note="这会向 DeepCoin 提交该指定仓位的市价全平订单。"' in button
    assert 'data-close-bound-position-status aria-live="polite"' in response.text


def test_bound_position_does_not_link_lifecycle_owned_by_another_binding(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=57,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            text="BTC long",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
        )
        session.add(candidate)
        session.flush()
        position_binding = ExecutionBinding(
            strategy_instance_id="position-owner",
            kol_id="group:100",
            chat_id=100,
            message_id=57,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-owner-only",
            status="active",
        )
        other_binding = ExecutionBinding(
            strategy_instance_id="lifecycle-owner",
            kol_id="group:999",
            chat_id=999,
            message_id=999,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            status="active",
        )
        session.add_all([position_binding, other_binding])
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=position_binding.id,
                strategy_instance_id=position_binding.strategy_instance_id,
                leg_index=0,
                purpose="entry",
                order_kind="market",
                pos_id="pos-owner-only",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            )
        )
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=other_binding.id,
                chat_id=100,
                message_id=57,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
            )
        )
        session.commit()
        position_binding_id = position_binding.id

    class MatchingPositionClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "posId": "pos-owner-only",
                    "posSide": "long",
                    "pos": "0.01",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

    response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=MatchingPositionClient,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    assert "已验证归属" in response.text
    assert "data-strategy-record-link" not in response.text
    assert 'href="/strategy-records/' not in response.text

    with session_factory() as session:
        existing_lifecycle = session.query(StrategyLifecycle).one()
        existing_lifecycle.execution_binding_id = position_binding_id
        second_message = RawMessage(
            chat_id=101,
            message_id=58,
            posted_at=datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
            text="ETH short",
        )
        session.add(second_message)
        session.flush()
        second_candidate = SignalCandidate(
            raw_message_id=second_message.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
        )
        session.add(second_candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=second_candidate.id,
                execution_binding_id=position_binding_id,
                chat_id=101,
                message_id=58,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=second_message.posted_at,
            )
        )
        session.commit()

    ambiguous_response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=MatchingPositionClient,
        )
    ).get("/positions-panel")

    assert ambiguous_response.status_code == 200
    assert "data-strategy-record-link" not in ambiguous_response.text
    assert 'href="/strategy-records/' not in ambiguous_response.text


def test_recognized_message_links_to_its_single_authoritative_lifecycle(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=88,
            posted_at=datetime(2026, 7, 17, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            confidence=0.91,
        )
        session.add(candidate)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=100,
            message_id=88,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=raw_message.posted_at,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/messages")

    assert response.status_code == 200
    assert f'href="/strategy-records/{lifecycle_id}"' in response.text
    assert f'data-message-strategy-record="{lifecycle_id}"' in response.text


def test_message_does_not_borrow_lifecycle_from_a_different_candidate(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=89,
            posted_at=datetime(2026, 7, 17, 8, 1, tzinfo=UTC),
            text="BTC long",
        )
        session.add(raw_message)
        session.flush()
        selected_candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            confidence=0.95,
        )
        lifecycle_candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            confidence=0.80,
        )
        session.add_all([selected_candidate, lifecycle_candidate])
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=lifecycle_candidate.id,
                chat_id=100,
                message_id=89,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=raw_message.posted_at,
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/100/messages"
    )

    assert response.status_code == 200
    assert "data-message-strategy-record" not in response.text
    assert 'href="/strategy-records/' not in response.text


def test_message_fails_closed_when_selected_candidate_has_multiple_lifecycles(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=90,
            posted_at=datetime(2026, 7, 17, 8, 2, tzinfo=UTC),
            text="BTC long",
        )
        session.add(raw_message)
        session.flush()
        selected_candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            confidence=0.95,
        )
        session.add(selected_candidate)
        session.flush()
        # Malformed legacy rows can point the same selected candidate at
        # multiple lifecycle identities while satisfying the database's
        # (chat_id, message_id) uniqueness constraint. The UI must not choose.
        session.add_all(
            [
                StrategyLifecycle(
                    signal_candidate_id=selected_candidate.id,
                    chat_id=100,
                    message_id=90,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=raw_message.posted_at,
                ),
                StrategyLifecycle(
                    signal_candidate_id=selected_candidate.id,
                    chat_id=101,
                    message_id=91,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="cancelled",
                    signal_at=raw_message.posted_at,
                ),
            ]
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/100/messages"
    )

    assert response.status_code == 200
    assert "data-message-strategy-record" not in response.text
    assert 'href="/strategy-records/' not in response.text


def test_reviewed_equivalent_positions_render_miya_and_deterministic_provenance(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:9527:56:ETH:short",
            kol_id="group:9527",
            chat_id=9527,
            message_id=56,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            pos_id="pos-miya-1,pos-miya-2",
            margin_mode="cross",
            position_mode="split",
            payload_json=json.dumps(
                {
                    "draft": {
                        "stop_loss": 1820.0,
                        "take_profit_legs": [{"price": 1700.0}],
                    }
                }
            ),
            status="active",
        )
        session.add(binding)
        session.flush()
        legs = [
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=index,
                purpose="entry",
                order_kind="trigger_limit",
                pos_id=pos_id,
                venue="deepcoin",
                attribution_status="verified",
                status="active",
                request_json=json.dumps(
                    {
                        "instId": "ETH-USDT-SWAP",
                        "posSide": "short",
                        "sz": "1.5",
                        "px": "1770",
                    }
                ),
            )
            for index, pos_id in enumerate(("pos-miya-1", "pos-miya-2"), start=1)
        ]
        session.add_all(legs)
        session.flush()
        evidence = {
            "policy_version": 2,
            "evidence_type": "equivalent_permutation_assignment",
            "component_leg_ids": [leg.id for leg in legs],
            "component_position_ids": ["pos-miya-1", "pos-miya-2"],
            "mapping_basis": "stable_sorted_canonicalization",
            "ownership_statement": (
                "binding owner proven; parent-child mapping canonicalized"
            ),
            "equivalence_signature": {
                "binding_id": binding.id,
                "strategy_instance_id": binding.strategy_instance_id,
                "venue": "deepcoin",
                "symbol": "ETH-USDT-SWAP",
                "side": "short",
                "requested_size": 1.5,
                "entry_price": 1770.0,
                "stop_loss": 1820.0,
                "take_profits": [1700.0],
                "protection_mutated": False,
                "margin_mode": "cross",
                "position_mode": "split",
                "order_kind": "trigger_limit",
                "leg_population": [
                    {
                        "leg_id": leg.id,
                        "binding_id": binding.id,
                        "strategy_instance_id": binding.strategy_instance_id,
                        "venue": "deepcoin",
                        "symbol": "ETH-USDT-SWAP",
                        "side": "short",
                        "requested_size": 1.5,
                        "entry_price": 1770.0,
                        "stop_loss": 1820.0,
                        "take_profits": [1700.0],
                        "margin_mode": "cross",
                        "position_mode": "split",
                        "order_kind": "trigger_limit",
                        "protection_mutated": False,
                    }
                    for leg in legs
                ],
                "position_population": [
                    {
                        "position_id": pos_id,
                        "symbol": "ETH-USDT-SWAP",
                        "side": "short",
                        "size": 1.5,
                        "entry_price": 1770.0,
                        "stop_loss": 1820.0,
                        "take_profits": [1700.0],
                        "margin_mode": "cross",
                        "position_mode": "split",
                    }
                    for pos_id in ("pos-miya-1", "pos-miya-2")
                ],
            },
        }
        for leg in legs:
            leg.attribution_evidence_json = json.dumps(evidence)
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": pos_id,
                    "posSide": "short",
                    "pos": "1.5",
                    "avgPx": "1770",
                }
                for pos_id in ("pos-miya-1", "pos-miya-2")
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="米娅 vip 会员群 11分组",
                        chat_id=9527,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=FakeDeepcoinClient,
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "米娅 vip 会员群 11分组" in response.text
    assert "等价腿确定性归属" in response.text
    assert "已审核等价腿组件，按稳定排序确定腿/仓位映射" in response.text
    assert "Deepcoin 直接 ID 证明" not in response.text
    assert "equivalent_permutation_assignment" not in response.text

    with session_factory() as session:
        session.query(ExecutionOrderLeg).filter_by(leg_index=2).one().pos_id = (
            "stale-pos-miya-2"
        )
        session.commit()

    stale_response = client.get("/positions-panel")

    assert stale_response.status_code == 200
    assert "等价腿确定性归属" not in stale_response.text
    assert "归属待确认" in stale_response.text
    assert "等价腿归属证据不完整或已过期" in stale_response.text


def test_conflicted_position_renders_frozen_persisted_attribution(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:56:BTC:long",
            kol_id="group:100",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id=None,
            status="unknown",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="trigger-56",
                pos_id="pos-conflict",
                venue="deepcoin",
                attribution_status="attribution_conflict",
                attribution_evidence_json=json.dumps(
                    {"candidate_leg_ids": [1, 2], "evidence_type": "trigger_fill"}
                ),
                status="active",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "posId": "pos-conflict",
                    "posSide": "long",
                    "pos": "0.01",
                    "avgPx": "62400",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

    response = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=FakeDeepcoinClient,
        )
    ).get("/positions-panel")

    assert response.status_code == 200
    assert "归属待确认" in response.text
    assert "归属冲突" in response.text
    assert "自动管理已冻结" in response.text
    assert "data-close-bound-position" not in response.text


def test_exchange_current_order_candidate_attribution(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=200,
            message_id=10,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="bravo",
            text="BTC long 62400-62500 SL 61800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400-62500",
            stop_loss_text="61800",
            take_profit_text="63600",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=200,
                message_id=10,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62500,
                stop_loss=61800,
                take_profit="63600",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "candidate-order-1",
                    "side": "long",
                    "ordType": "limit",
                    "state": "live",
                    "px": "62420",
                    "sz": "10",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Bravo Group",
                        chat_id=200,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "可能归属" in response.text
    assert "Bravo Group" in response.text
    assert "BTC long entry 62400-62500" in response.text
    assert "order candidate-order-1" in response.text
    assert 'data-exchange-group-section' in response.text


def test_exchange_current_order_uses_execution_binding_attribution(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=210,
            message_id=20,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="charlie",
            text="BTC long 60700 SL 59800 TP 62000",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="60700",
            stop_loss_text="59800",
            take_profit_text="62000",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            ExecutionBinding(
                kol_id="group:210",
                chat_id=210,
                message_id=20,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                order_id="bound-open-order-1",
                status="open",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "bound-open-order-1",
                    "side": "buy",
                    "ordType": "limit",
                    "state": "live",
                    "px": "60700",
                    "sz": "5",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Charlie Group",
                        chat_id=210,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "order bound-open-order-1" in response.text
    assert "已绑定" in response.text
    assert "Charlie Group" in response.text


def test_exchange_history_order_uses_verified_entry_leg_pos_id_before_candidates(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        wrong_raw = RawMessage(
            chat_id=211,
            message_id=21,
            posted_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
            sender_name="wrong candidate",
            text="ETH short entry 1816-1888 SL 1918 TP 1796/1768",
        )
        session.add(wrong_raw)
        session.flush()
        wrong_candidate = SignalCandidate(
            raw_message_id=wrong_raw.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
            entry_text="1816-1888",
            stop_loss_text="1918",
            take_profit_text="1796/1768",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(wrong_candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=wrong_candidate.id,
                chat_id=211,
                message_id=21,
                symbol="ETH",
                side="short",
                lifecycle_status="exited",
                signal_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
                entry_range_low=1816,
                entry_range_high=1888,
                stop_loss=1918,
                take_profit="1796/1768",
            )
        )
        binding = ExecutionBinding(
            kol_id="group:212",
            chat_id=212,
            message_id=22,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            order_id="trigger-parent-order",
            client_order_id="client-leg-1",
            pos_id="history-fill-order",
            status="active",
            strategy_instance_id="deepcoin:212:22:ETH:short",
        )
        session.add(binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="trigger-parent-order",
                client_order_id="client-leg-1",
                pos_id="history-fill-order",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=212,
                message_id=22,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 20, 8, 5, tzinfo=UTC),
                entered_at=datetime(2026, 7, 20, 8, 13, tzinfo=UTC),
                entry_range_low=1883,
                entry_range_high=1893,
                stop_loss=1900,
                take_profit="1860/1840/1810",
                execution_binding_id=binding.id,
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "history-fill-order",
                    "posSide": "short",
                    "side": "sell",
                    "ordType": "limit",
                    "state": "filled",
                    "px": "1888",
                    "sz": "6.2",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(chat_title="Wrong Candidate", chat_id=211),
                    TargetGroupConfig(chat_title="Verified Leg Group", chat_id=212),
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "order history-fill-order" in response.text
    assert "已验证归属" in response.text
    assert "Verified Leg Group" in response.text
    assert "entry leg #1" in response.text
    assert "可能归属" not in response.text


def test_exchange_current_tpsl_order_uses_protection_ledger_attribution(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:220",
            chat_id=220,
            message_id=30,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            order_id="entry-order-1",
            pos_id="pos-eth-1",
            status="active",
            strategy_instance_id="deepcoin:220:30:ETH:long",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="entry-order-1",
            pos_id="pos-eth-1",
            venue="deepcoin",
            attribution_status="verified",
            status="active",
        )
        session.add(leg)
        session.flush()
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="pos-eth-1",
                instrument_id="ETH-USDT-SWAP",
                side="long",
                order_id="tpsl-verified-1",
                purpose="take_profit",
                trigger_price="1955",
                size_text="0",
                status="verified",
                evidence_source="entry_protection_response",
                evidence_json='{"match":"exchange_returned_order_id"}',
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "tpsl-verified-1",
                    "posSide": "long",
                    "side": "sell",
                    "triggerOrderType": "TPSL",
                    "tpTriggerPrice": "1955",
                    "sz": "0",
                }
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Ledger Group",
                        chat_id=220,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "order tpsl-verified-1" in response.text
    assert "已验证保护" in response.text
    assert "Ledger Group" in response.text
    assert "pos pos-eth-1" in response.text


def test_exchange_current_tpsl_order_without_ledger_is_not_candidate_attributed(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=230,
            message_id=40,
            posted_at=datetime(2026, 7, 18, 7, 47, tzinfo=UTC),
            sender_name="candidate group",
            text="ETH long 1844 SL 1788 TP 1955",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="ETH",
                side="long",
                event_type="entry_signal",
                entry_text="1844",
                stop_loss_text="1788",
                take_profit_text="1955",
                parse_source="mimo_authoritative",
                confidence=0.94,
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=230,
                message_id=40,
                symbol="ETH",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 18, 7, 47, tzinfo=UTC),
                entry_range_low=1844,
                entry_range_high=1844,
                stop_loss=1788,
                take_profit="1955",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "tpsl-unverified-1",
                    "posSide": "long",
                    "side": "sell",
                    "triggerOrderType": "TPSL",
                    "tpTriggerPrice": "1955",
                    "sz": "0",
                }
            ]

        def list_trigger_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Candidate Group",
                        chat_id=230,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "order tpsl-unverified-1" in response.text
    assert "保护归属未验证" in response.text
    assert "可能归属" not in response.text
    assert "Candidate Group" not in response.text


def test_exchange_tpsl_order_row_uses_non_zero_trigger_price():
    row = _exchange_order_row(
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "tpsl-order-1",
            "posSide": "long",
            "side": "sell",
            "ordPx": "0",
            "triggerPx": "0",
            "slTriggerPrice": "0",
            "tpTriggerPrice": "64100",
            "triggerOrderType": "TPSL",
            "sz": "0",
        },
        source="触发委托",
    )

    assert row["price_text"] == "64100"
    assert row["side"] == "long"
    assert row["order_direction_label"] == "止盈止损/平多"
    assert row["order_direction_side"] == "short"


def test_exchange_order_row_uses_deepcoin_app_direction_labels():
    short_tpsl = _exchange_order_row(
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "tpsl-short-1",
            "posSide": "short",
            "side": "buy",
            "triggerOrderType": "TPSL",
            "tpTriggerPrice": "62600",
            "slTriggerPrice": "63600",
        },
        source="触发委托",
    )
    long_conditional = _exchange_order_row(
        {
            "instId": "ETH-USDT-SWAP",
            "ordId": "conditional-long-1",
            "posSide": "long",
            "side": "buy",
            "triggerOrderType": "Conditional",
            "triggerPx": "1720",
        },
        source="触发委托",
    )
    short_conditional = _exchange_order_row(
        {
            "instId": "ETH-USDT-SWAP",
            "ordId": "conditional-short-1",
            "posSide": "short",
            "side": "sell",
            "triggerOrderType": "Conditional",
            "triggerPx": "1767.5",
        },
        source="触发委托",
    )

    assert short_tpsl["order_direction_label"] == "止盈止损/平空"
    assert short_tpsl["order_direction_side"] == "long"
    assert long_conditional["order_direction_label"] == "条件/开多"
    assert long_conditional["order_direction_side"] == "long"
    assert short_conditional["order_direction_label"] == "条件/开空"
    assert short_conditional["order_direction_side"] == "short"


def test_exchange_order_row_formats_deepcoin_times_in_china_timezone():
    row = _exchange_order_row(
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "conditional-time-1",
            "posSide": "long",
            "side": "buy",
            "triggerOrderType": "Conditional",
            "cTime": "1783004912000",
            "uTime": "1783009610",
        },
        source="触发委托",
    )

    assert row["created_at"] == "2026-07-02 23:08:32"
    assert row["updated_at"] == "2026-07-03 00:26:50"


def test_exchange_unmatched_order_stays_unassigned(tmp_path):
    database_path = tmp_path / "research.db"

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "ETHUSDT",
                    "ordId": "unmatched-order-1",
                    "side": "short",
                    "ordType": "limit",
                    "state": "live",
                    "px": "2500",
                    "sz": "1",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/positions-panel")

    assert response.status_code == 200
    assert "未归属" in response.text
    assert "order unmatched-order-1" in response.text
    assert 'data-exchange-group-section' in response.text


def test_index_page_renders_media_as_compact_preview_links(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=1,
            posted_at=datetime(2026, 4, 2, tzinfo=UTC),
            sender_name="Demo Group",
            text="hello media",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="data/media/77/1.jpg",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    # Media previews now lazy-loaded in messages tab
    msgs_resp = client.get("/groups/77/detail/tab/messages")
    assert msgs_resp.status_code == 200
    assert 'class="media-link"' in msgs_resp.text
    assert 'class="message-media-preview"' in msgs_resp.text


def test_index_page_renders_message_cards_with_hierarchy_and_media_labels(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=2,
            posted_at=datetime(2026, 4, 2, 12, 30, tzinfo=UTC),
            sender_name="娆ч槼鐏婊氫粨鐝煔€ 11鍒嗙粍",
            text="澶氬崟缁х画鎸佹湁\n璁剧疆濂芥鎹熺偣",
        )
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="messagemediaphoto",
                    local_path="data/media/77/2.jpg",
                ),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="messagemediadocument",
                    mime_type="video/mp4",
                    local_path="data/media/77/2.mp4",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    # Messages are lazy-loaded 鈥?test via tab endpoint
    response = client.get("/groups/77/detail/tab/messages")

    assert response.status_code == 200
    assert 'class="message-card-header"' in response.text
    assert 'class="message-sender-name"' in response.text
    assert 'class="message-posted-at"' in response.text
    assert 'class="message-id-badge"' in response.text
    assert 'class="message-card-body"' in response.text
    assert 'class="message-text"' in response.text
    assert "娆ч槼鐏婊氫粨鐝煔€ 11鍒嗙粍" in response.text
    assert "澶氬崟缁х画鎸佹湁" in response.text
    assert "/local-media/77/2.jpg" in response.text
    assert 'class="media-token"' in response.text
    assert "/local-media/77/2.mp4" not in response.text


def test_message_detail_renders_context_analysis_backfill_as_non_executing_history(
    tmp_path,
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw = RawMessage(chat_id=77, message_id=1463, text="历史上下文补齐")
        session.add(raw)
        session.flush()
        attempt = ContextResolutionAttempt(
            raw_message_id=raw.id,
            context_fingerprint="sha256:web-history",
            model="deepseek-v4-flash",
            prompt_versions_json='{"context_resolution":"context-resolution-v1"}',
            request_summary_json="{}",
            status="exhausted",
        )
        session.add(attempt)
        session.flush()
        session.add(
            ContextAnalysisBackfill(
                run_id="web-history-run",
                raw_message_id=raw.id,
                source_attempt_id=attempt.id,
                source_request_sha256="request-hash",
                prompt_version="context-resolution-v1",
                analyst_model="codex-manual-context-v1",
                decision_json='{"decision":"manage_thread","target_thread_ids":[123],"confidence":0.91,"reason":"仅补齐历史上下文"}',
                status="analysis_only_completed",
            )
        )
        session.commit()
    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[TargetGroupConfig(chat_title="77", chat_id=77)]
            ),
        )
    )

    response = client.get("/groups/77/detail/tab/messages")

    assert response.status_code == 200
    assert "历史分析补齐（不执行）" in response.text
    assert "manage_thread" in response.text
    assert "仅补齐历史上下文" in response.text
    assert "线程 #123" in response.text
    assert "data-message-strategy-record" not in response.text


def test_message_tab_renders_bounded_adjacent_entry_assembly(tmp_path):
    database_path = tmp_path / "entry-assembly-render.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(RawMessage(chat_id=77, message_id=9902, text="BTC strategy"))
        session.add(
            ExecutionBinding(
                strategy_instance_id="render-entry-v2",
                kol_id="chen",
                chat_id=77,
                message_id=9902,
                symbol="BTCUSDT",
                side="long",
                venue="deepcoin",
                payload_json=json.dumps({"draft": {"entry_preamble_assembly": {
                    "mode": "live", "status": "assembled",
                    "configured_risk_budget_usdt": 20,
                    "risk_multiplier": "0.5",
                    "effective_risk_budget_usdt": 10,
                    "strategy_message_id": 9902,
                    "fragment_ids": [11, 12],
                    "allocations": [0.5, 0.5],
                    "supplemental_prices": [63400],
                    "api_key": "secret-value",
                }}}),
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/77/detail/tab/messages"
    )

    assert response.status_code == 200
    assert "配置20U × 50% = 实际风险预算10U" in response.text
    assert "整单100%；两档各50%" in response.text
    assert "补仓价 63400" in response.text
    assert "secret-value" not in response.text


def test_message_detail_renders_authoritative_semantic_review_states(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    states = [
        ("completed", "none", "agreed", "一致", "equivalent", []),
        (
            "completed",
            "normal",
            "disagreed",
            "普通差异",
            "止盈细节不同",
            ["non_material_price_detail"],
        ),
        (
            "completed",
            "critical",
            "disagreed",
            "严重分歧",
            "方向冲突",
            ["action_family"],
        ),
        ("completed", None, "agreed", "待重新复核", None, []),
        ("execution_pending", None, "pending", "等待中", None, []),
        ("execution_running", None, "pending", "等待中", None, []),
        ("failed", None, "pending", "失败", None, []),
    ]
    with session_factory() as session:
        for index, (
            status,
            severity,
            agreement_status,
            _label,
            reason,
            conflict_types,
        ) in enumerate(states, 1):
            raw_message = RawMessage(chat_id=77, message_id=index, text=f"message {index}")
            session.add(raw_message)
            session.flush()
            session.add(
                RecognitionDecision(
                    raw_message_id=raw_message.id,
                    input_kind="text",
                    authoritative_model="mimo-v2.5",
                    authoritative_status="是策略",
                    authoritative_payload_json="{}",
                    agreement_status=agreement_status,
                    differences_json="[]",
                    comparison_status=status,
                    disagreement_severity=severity,
                    comparison_model="deepseek-v4-flash",
                    comparison_payload_json=json.dumps(
                        {
                            "reason": reason,
                            "conflict_types": conflict_types,
                            "raw_provider_response": "never-render-provider-secret",
                            "notification_claim_token": "never-render-frozen-token",
                        },
                        ensure_ascii=False,
                    ),
                    comparison_error="provider timeout" if status == "failed" else None,
                )
            )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/77/detail/tab/messages"
    )

    assert response.status_code == 200
    for _status, _severity, _agreement, label, _reason, _conflicts in states:
        assert f"AI复核：{label}" in response.text
    assert 'class="semantic-review semantic-review-critical"' in response.text
    assert 'role="alert"' in response.text
    normal_review = re.search(
        r'<details class="semantic-review semantic-review-normal"(.*?)</details>',
        response.text,
        re.S,
    )
    assert normal_review is not None
    assert " open" not in normal_review.group(1).split(">", 1)[0]
    assert "止盈细节不同" in normal_review.group(1)
    assert "non_material_price_detail" in normal_review.group(1)
    assert "权威模型结论" in response.text
    assert "MiMo 主分析" in response.text
    assert "DeepSeek 辅助复核" in response.text
    assert "历史实验（非权威）" not in response.text
    assert "never-render-provider-secret" not in response.text
    assert "never-render-frozen-token" not in response.text
    assert "provider timeout" not in response.text
    assert "历史记录没有语义分歧等级，需重新复核" in response.text


def test_message_detail_renders_review_disabled_without_deepseek_claim(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw = RawMessage(chat_id=77, message_id=88, text="BTC short")
        session.add(raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="review_disabled",
                differences_json="[]",
                comparison_status="completed",
                comparison_model="historical-deepseek-model",
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/77/detail/tab/messages"
    )

    assert response.status_code == 200
    assert "AI复核：辅助复核已关闭" in response.text
    assert "复核模型：historical-deepseek-model" not in response.text
    assert "AI复核：一致" not in response.text
    assert "AI复核：失败" not in response.text
    assert "AI复核：等待中" not in response.text
    assert 'role="alert"' not in response.text


def test_critical_semantic_review_opens_outer_ai_disclosure_for_non_strategy(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=20,
            text="not actionable",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MessageRecognition(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="MiMo 权威结果为非策略",
                engine="mimo-v2.5",
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="disagreed",
                differences_json='["actionability"]',
                comparison_status="completed",
                disagreement_severity="critical",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json=json.dumps(
                    {
                        "reason": "语义复核认为存在紧急退出动作",
                        "conflict_types": ["urgent_exit_missed"],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/77/detail/tab/messages"
    )

    assert response.status_code == 200
    assert re.search(
        r'<details\s+class="message-ai-insights is-not-strategy is-decision-card-history"\s+'
        r'data-message-ai-insights\s+open\s*>',
        response.text,
    )
    assert re.search(
        r'<details class="semantic-review semantic-review-critical"\s+'
        r'open role="alert" aria-label="AI复核：严重分歧"',
        response.text,
    )


def test_context_semantic_review_renders_open_without_critical_alert(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=21,
            text="多单移动止损至开仓价",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MessageRecognition(
                raw_message_id=raw_message.id,
                status="非策略",
                reason="管理已有仓位",
                engine="mimo-v2.5",
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json="{}",
                agreement_status="disagreed",
                differences_json='["target_lifecycle_id", "symbol"]',
                comparison_status="completed",
                disagreement_severity="critical",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json=json.dumps(
                    {
                        "reason": "当前消息未指定目标生命周期，独立判断无法确认504。",
                        "conflict_types": ["symbol", "target_lifecycle"],
                        "independent_action": {
                            "action_type": "position_update",
                            "symbol": None,
                            "side": "long",
                            "target_lifecycle_id": None,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

    response = TestClient(create_web_app(database_path=database_path)).get(
        "/groups/77/detail/tab/messages"
    )

    assert response.status_code == 200
    assert "AI复核：上下文待核对" in response.text
    assert 'class="semantic-review semantic-review-context"' in response.text
    assert 'aria-label="AI复核：上下文待核对"' in response.text
    assert 'role="alert" aria-label="AI复核：严重分歧"' not in response.text


def test_management_batch_panel_is_read_only_and_has_safety_labels(tmp_path):
    response = TestClient(create_web_app(database_path=tmp_path / "research.db")).get("/")
    assert response.status_code == 200
    assert 'data-workbench-panel="management-batches"' in response.text
    assert 'data-management-batches-panel' in response.text
    assert "策略管理批次" in response.text
    assert "未调用交易 API" in response.text
    assert "禁止自动重试" in response.text
    for forbidden in ('data-management-retry', 'data-management-close', 'data-management-cancel'):
        assert forbidden not in response.text


def test_index_page_versions_static_assets_to_avoid_stale_browser_cache(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    response = client.get("/")

    assert response.status_code == 200
    assert "/static/app.css?v=" in response.text
    assert "/static/app.js?v=" in response.text
    assert 'data-workbench-asset-version="' in response.text


def test_trading_symbol_capability_ui_is_explicit_and_renders_errors_safely(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    html = client.get("/more-panel").text
    js = client.get("/static/app.js").text
    css = client.get("/static/app.css").text
    selector_start = js.index("function initTradingSymbolSelector")
    selector_end = js.index("\nfunction parseSymbolList", selector_start)
    selector = js[selector_start:selector_end]

    assert "data-contract-spec-status" in html
    assert "venue_supported" in selector
    assert "venue_state" in selector
    assert "spec_status" in selector
    assert "tradable" in selector
    assert "reason_code" in selector
    assert "fetched_at" in selector
    assert "expires_at" in selector
    assert "Deepcoin 不可交易" in selector
    assert "选中仅表示全局允许，不会覆盖 Deepcoin 能力" in html
    assert "checkbox.disabled" not in selector
    assert ".slice(0, 240)" in js
    assert "contractSpecStatus.textContent" in selector
    assert "contractSpecStatus.innerHTML" not in selector
    assert ".symbol-capability-badge" in css
    assert ".is-tradable" in css
    assert ".is-non-tradable" in css


def test_trading_symbol_capability_view_recomputes_after_local_selection(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the capability view behavior test")
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    helper_start = js.index("function symbolCapabilityView")
    helper_end = js.index("\nfunction initTradingSymbolSelector", helper_start)
    helper_source = js[helper_start:helper_end]
    script = f"""
{helper_source}
const item = {{
  venue_supported: true,
  venue_state: 'live',
  spec_status: 'fresh',
  execution_mode: 'live',
  execution_reason_code: 'global_not_allowed',
}};
console.log(JSON.stringify([
  symbolCapabilityView(item, false),
  symbolCapabilityView(item, true),
]));
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    unselected, selected = json.loads(completed.stdout)
    assert unselected["tradable"] is False
    assert unselected["reason_code"] == "global_not_allowed"
    assert selected["tradable"] is True
    assert selected["reason_code"] == "tradable"
    assert selected["execution_tradable"] is True


def test_contract_spec_status_text_bounds_hostile_error_as_literal_text(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for the status rendering behavior test")
    js = TestClient(create_web_app(database_path=tmp_path / "research.db")).get(
        "/static/app.js"
    ).text
    helper_start = js.index("function boundedContractSpecStatusText")
    helper_end = js.index("\nfunction initTradingSymbolSelector", helper_start)
    helper_source = js[helper_start:helper_end]
    hostile = "<img src=x onerror=alert(1)>" + ("x" * 500)
    script = f"""
{helper_source}
console.log(JSON.stringify(boundedContractSpecStatusText({{
  state: 'unavailable',
  last_error: {json.dumps(hostile)},
}})));
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(completed.stdout)

    assert "<img src=x onerror=alert(1)>" in rendered
    assert len(rendered) <= len("Deepcoin 规格状态：unavailable；") + 240


def test_index_page_shows_actionable_session_lock_hint(tmp_path):
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            live_listener_status_reason=(
                "Telegram session data/telegram.session is already in use by another process. "
                "owner pid=12345 status=S command=telegram-kol-research web"
            ),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "session" in response.text.lower()
    assert "session-status" in response.text
    assert "session-release --pid 12345" in response.text
    assert "owner pid=12345" in response.text


def test_index_page_shows_recovery_decisions_for_manual_review(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="manual_review",
                    reason_codes=["current_price_in_entry_range"],
                    entry_range=(68000.0, 68200.0),
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    assert "data-trader-dashboard" in response.text
    strategy_response = client.get("/groups/100/strategy-mid-panel?filter=holding")
    assert strategy_response.status_code == 200
    assert 'data-strategy-filter="holding"' in strategy_response.text
    assert 'data-strategy-filter="pending"' in strategy_response.text

    # Recovery detail renders in the detail panel per-group
    detail_response = client.get("/groups/100/detail")
    assert detail_response.status_code == 200
    assert "data-recovery-status" in detail_response.text
    assert "data-recovery-status" in detail_response.text


def test_index_page_shows_recovery_execution_preview_queue(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["recovery_checks_passed"],
                    entry_range=(68000.0, 68200.0),
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    strategy_response = client.get("/groups/100/strategy-mid-panel?filter=pending")
    assert strategy_response.status_code == 200
    assert 'data-strategy-filter="pending"' in strategy_response.text

    # Execution preview renders in the pending tab (lazy-loaded)
    queue_response = client.get("/api/recovery-execution-queue")
    assert queue_response.status_code == 200
    payload = queue_response.json()
    assert payload["items"]
    item = payload["items"][0]
    assert item["symbol"] == "BTC"
    assert item["side"] == "long"
    assert item["entry_range_text"] == "68000-68200"
    assert item["payload_preview"]["open_side"] == "buy"


def test_strategy_mid_panel_shows_take_profit_for_entered_lifecycle(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=56,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600/64800",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400",
            stop_loss_text="60800",
            take_profit_text="63600/64800",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-56",
            pos_id="pos-56",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=56,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62400,
                stop_loss=60800,
                take_profit=None,
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "BTC" in response.text
    assert "62400" in response.text
    assert "60800" in response.text
    assert "63600/64800" in response.text
    assert "数量" in response.text
    assert "0.625 BTC" in response.text
    assert "止损1000U" in response.text
    assert 'data-strategy-card' in response.text
    assert 'class="strategy-card-summary"' in response.text
    assert "strategy-card-details" in response.text
    assert response.text.index('class="strategy-card-summary"') < response.text.index(
        "strategy-card-details"
    )
    summary_match = re.search(
        r'<summary class="strategy-card-summary">(.*?)</summary>',
        response.text,
        re.S,
    )
    assert summary_match
    summary_html = summary_match.group(1)
    assert "BTC" in summary_html
    assert "62400" in summary_html
    assert "63600/64800" in summary_html
    assert "60800" in summary_html
    assert "0.625 BTC" in summary_html
    assert "止损1000U" in summary_html
    assert "事件时间线" not in summary_html


def test_strategy_mid_panel_hides_unbound_entered_lifecycle_from_holding(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=58,
            posted_at=datetime(2026, 7, 1, 13, 56, tzinfo=UTC),
            sender_name="chen",
            text="BTC short 59400-59800 SL 60900 TP 57800/56000",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=58,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                entry_range_low=59400,
                entry_range_high=59800,
                stop_loss=60900,
                take_profit="57800/56000",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "BTC" not in response.text
    assert re.search(r'class="strategy-section-count">\s*0\s*</span>', response.text)


def test_strategy_mid_panel_shows_exited_lifecycle_filter(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=57,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400",
            stop_loss_text="60800",
            take_profit_text="63600",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=57,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="take_profit",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                exited_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62400,
                entry_price_actual=62400,
                exit_price_actual=63600,
                stop_loss=60800,
                take_profit="63600",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert 'data-strategy-filter="exited"' in response.text
    assert 'data-strategy-filter="exited"' in response.text
    assert "BTC" in response.text
    assert "63600" in response.text
    assert "63600" in response.text


def test_strategy_mid_panel_keeps_multiple_exited_same_symbol_side(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        for message_id, entry_price, exit_price in [
            (57, 62400, 63600),
            (58, 62500, 63700),
        ]:
            raw_message = RawMessage(
                chat_id=100,
                message_id=message_id,
                posted_at=datetime(2026, 6, 12, 8, message_id - 57, tzinfo=UTC),
                sender_name="alice",
                text=f"BTC long Entry {entry_price} SL 60800 TP {exit_price}",
            )
            session.add(raw_message)
            session.flush()
            candidate = SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text=str(entry_price),
                stop_loss_text="60800",
                take_profit_text=str(exit_price),
                parse_source="text_ai",
                confidence=0.91,
                review_status="pending",
            )
            session.add(candidate)
            session.flush()
            session.add(
                StrategyLifecycle(
                    signal_candidate_id=candidate.id,
                    chat_id=100,
                    message_id=message_id,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="exited",
                    exit_reason="take_profit",
                    signal_at=raw_message.posted_at,
                    entered_at=raw_message.posted_at,
                    exited_at=raw_message.posted_at,
                    entry_range_low=entry_price,
                    entry_range_high=entry_price,
                    entry_price_actual=entry_price,
                    exit_price_actual=exit_price,
                    stop_loss=60800,
                    take_profit=str(exit_price),
                )
            )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert response.text.count("<strong>BTC</strong>") == 2
    assert "62400" in response.text
    assert "62500" in response.text
    assert "63600" in response.text
    assert "63700" in response.text


def test_strategy_mid_panel_shows_cancelled_order_lifecycle(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=374,
            posted_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            sender_name="mia",
            text=(
                "米娅BTC短线合约交易策略 做空（限价） "
                "进场点位：63200-63500 止损点位：64200 止盈点位：62000"
            ),
        )
        cancel_message = RawMessage(
            chat_id=100,
            message_id=376,
            posted_at=datetime(2026, 6, 19, 9, 24, tzinfo=UTC),
            sender_name="mia",
            text="取消限价，等我后续信号！",
        )
        session.add_all([original_message, cancel_message])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="63200-63500",
            stop_loss_text="64200",
            take_profit_text="62000",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=374,
                symbol="BTC",
                side="short",
                lifecycle_status="exited",
                exit_reason="cancelled",
                exit_signal_message_id=376,
                signal_at=original_message.posted_at,
                exited_at=cancel_message.posted_at,
                entry_range_low=63200,
                entry_range_high=63500,
                stop_loss=64200,
                take_profit="62000",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert "取消挂单" in response.text
    assert "原策略" in response.text
    assert "#374" in response.text
    assert "最新事件" in response.text
    assert "#376" in response.text
    assert "pending_entry → cancelled" in response.text
    assert "取消限价，等我后续信号！" in response.text
    assert "离场确认时间" in response.text
    assert "2026-06-19 17:24:00 UTC+8" in response.text


def test_strategy_mid_panel_shows_market_entry_confirmation_event(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=374,
            posted_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            sender_name="mia",
            text="BTC 做空 限价 63200-63500 止损 64200 止盈 62000",
        )
        entry_message = RawMessage(
            chat_id=100,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            sender_name="mia",
            text="BTC 现价 63320 入场",
        )
        session.add_all([original_message, entry_message])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="63200-63500",
            stop_loss_text="64200",
            take_profit_text="62000",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=374,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            order_id="order-374",
            pos_id="pos-374",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=374,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                entry_signal_message_id=377,
                signal_at=original_message.posted_at,
                entered_at=entry_message.posted_at,
                entry_range_low=63200,
                entry_range_high=63500,
                entry_price_actual=63320,
                stop_loss=64200,
                take_profit="62000",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "原策略" in response.text
    assert "#374" in response.text
    assert "最新事件" in response.text
    assert "#377" in response.text
    assert "入场确认" in response.text
    assert "BTC 现价 63320 入场" in response.text
    assert "策略消息接收时间" in response.text
    assert "策略创建/识别时间" in response.text
    assert "入场确认时间" in response.text
    assert "最新事件时间" in response.text
    assert "2026-06-19 12:29:00 UTC+8" in response.text
    assert "2026-06-19 17:40:00 UTC+8" in response.text


def test_strategy_mid_panel_shows_position_management_event(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=1395,
            posted_at=datetime(2026, 6, 17, 10, 26, tzinfo=UTC),
            sender_name="nick",
            text="BTC 63800-61800附近做多，均价62800，65500-66500-67500止盈，止损61000",
        )
        management_message = RawMessage(
            chat_id=100,
            message_id=1400,
            posted_at=datetime(2026, 6, 18, 8, 36, tzinfo=UTC),
            sender_name="nick",
            text="现价64500附近提前止盈一半带保护",
        )
        session.add_all([original_message, management_message])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="63800-61800",
            stop_loss_text="61000",
            take_profit_text="65500/66500/67500",
            parse_source="text_ai",
            confidence=0.95,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=1395,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-1395",
            pos_id="pos-1395",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=1395,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=original_message.posted_at,
                entered_at=datetime(2026, 6, 18, 4, 11, tzinfo=UTC),
                entry_range_low=61800,
                entry_range_high=63800,
                entry_price_actual=63794.4,
                stop_loss=61000,
                take_profit="65500/66500/67500",
                management_signal_message_id=1400,
                management_action="partial_take_profit",
                management_note="当前消息要求提前止盈一半并带保护，属于持仓管理更新",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "部分止盈" in response.text
    assert "#1400" in response.text
    assert "提前止盈一半带保护" in response.text
    assert "持仓中" in response.text
    assert "最新事件时间" in response.text
    assert "2026-06-18 16:36:00 UTC+8" in response.text


def test_strategy_mid_panel_shows_chronological_lifecycle_events(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=2225,
            posted_at=datetime(2026, 6, 26, 3, 42, 37, tzinfo=UTC),
            sender_name="ouyang",
            text="ETH 市价进场 1535 止损 1500 止盈 1615",
        )
        partial_message = RawMessage(
            chat_id=100,
            message_id=2229,
            posted_at=datetime(2026, 6, 26, 3, 52, 14, tzinfo=UTC),
            sender_name="ouyang",
            text="现目前多单盈利18个点，分批止盈30%，多单继续持有！",
        )
        hold_message = RawMessage(
            chat_id=100,
            message_id=2233,
            posted_at=datetime(2026, 6, 26, 6, 18, 46, tzinfo=UTC),
            sender_name="ouyang",
            text="现目前多单获利11点，多单继续持有，等待拉升！",
        )
        session.add_all([original_message, partial_message, hold_message])
        session.flush()
        entry_candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            entry_text="1535",
            stop_loss_text="1500",
            take_profit_text="1615",
            parse_source="text_ai",
            confidence=0.95,
            review_status="pending",
        )
        partial_candidate = SignalCandidate(
            raw_message_id=partial_message.id,
            symbol="ETH",
            side="long",
            event_type="position_update",
            stop_loss_text="1500",
            take_profit_text="1615",
            parse_source="lifecycle_ai",
            confidence=0.85,
            review_status="pending",
        )
        hold_candidate = SignalCandidate(
            raw_message_id=hold_message.id,
            symbol="ETH",
            side="long",
            event_type="position_update",
            stop_loss_text="1500",
            take_profit_text="1615",
            parse_source="lifecycle_ai",
            confidence=0.85,
            review_status="pending",
        )
        session.add_all([entry_candidate, partial_candidate, hold_candidate])
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=entry_candidate.id,
                chat_id=100,
                message_id=2225,
                symbol="ETH",
                side="long",
                lifecycle_status="exited",
                exit_reason="stop_loss",
                signal_at=original_message.posted_at,
                entered_at=datetime(2026, 6, 26, 3, 43, tzinfo=UTC),
                exited_at=datetime(2026, 6, 26, 12, 39, tzinfo=UTC),
                entry_range_low=1535,
                entry_range_high=1535,
                entry_price_actual=1535,
                exit_price_actual=1535,
                stop_loss=1535,
                take_profit="1615",
                management_signal_message_id=2229,
                management_action="partial_take_profit",
                management_note="分批止盈30%，多单继续持有",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert "事件时间线" in response.text
    assert "原策略" in response.text
    assert "入场确认" in response.text
    assert "分批止盈30%" in response.text
    assert "多单继续持有，等待拉升" in response.text
    assert "止损触发" in response.text
    assert (
        response.text.index("#2225")
        < response.text.index("分批止盈30%")
        < response.text.index("多单继续持有，等待拉升")
        < response.text.index("止损触发")
    )
