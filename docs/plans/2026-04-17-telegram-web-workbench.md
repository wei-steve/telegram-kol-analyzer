# Telegram Web Workbench Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a local web workbench for Telegram research that shows groups and message timelines, adds a grounded AI chat panel via CLIProxyAPI, and supports near-real-time browser updates without duplicating or missing persisted messages.

**Architecture:** Add a thin FastAPI web layer inside the existing Python package. Reuse SQLite and existing SQLAlchemy models for data access, reuse message-centered serialization patterns from `dataset_export.py` for AI grounding, and combine SSE push with checkpoint-based reconciliation for resilient live updates.

**Tech Stack:** Python 3.12+, FastAPI, Jinja2, SQLAlchemy, httpx, Typer, Telethon, SQLite, Pytest

---

### Task 1: Add Web Dependencies and CLI Entry Point

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_web_cli.py`

**Step 1: Write the failing test**

```python
from typer.testing import CliRunner

from telegram_kol_research.cli import app


def test_web_command_is_available_in_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "web" in result.stdout
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_cli.py -v`
Expected: FAIL because the `web` command does not exist yet.

**Step 3: Write minimal implementation**

- Add dependencies for `fastapi`, `jinja2`, `uvicorn`, and `httpx` in `pyproject.toml`
- Add a `web` CLI command in `src/telegram_kol_research/cli.py` that imports and runs a web app entrypoint

Minimal command shape:

```python
@app.command()
def web(
    host: str = "127.0.0.1",
    port: int = 8000,
    database_path: Path = Path("data/research.db"),
) -> None:
    ...
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_cli.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add pyproject.toml src/telegram_kol_research/cli.py tests/test_web_cli.py
git commit -m "feat: add web command scaffold"
```

### Task 2: Create the FastAPI App Skeleton

**Files:**
- Create: `src/telegram_kol_research/web_app.py`
- Create: `tests/test_web_app.py`

**Step 1: Write the failing test**

```python
from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_root_page_renders_successfully(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_app.py -v`
Expected: FAIL because the web app module does not exist.

**Step 3: Write minimal implementation**

- Create `create_web_app(database_path)` in `src/telegram_kol_research/web_app.py`
- Register a root route that returns a placeholder HTML response
- Attach the resolved database path to the app state for later query access

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_app.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py tests/test_web_app.py
git commit -m "feat: add fastapi web app skeleton"
```

### Task 3: Add Group List Query Helpers

**Files:**
- Create: `src/telegram_kol_research/web_queries.py`
- Create: `tests/test_web_queries_groups.py`

**Step 1: Write the failing test**

```python
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.web_queries import load_group_rows


def test_load_group_rows_orders_groups_by_latest_message_desc(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=1, message_id=1, posted_at=datetime(2026, 4, 1, tzinfo=UTC), text="older"),
                RawMessage(chat_id=2, message_id=1, posted_at=datetime(2026, 4, 2, tzinfo=UTC), text="newer"),
            ]
        )
        session.commit()

    rows = load_group_rows(session_factory)

    assert [row["chat_id"] for row in rows] == [2, 1]
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_queries_groups.py -v`
Expected: FAIL because the query helper does not exist.

**Step 3: Write minimal implementation**

- Implement `load_group_rows(session_factory)`
- Aggregate `raw_messages` by `chat_id`
- Return `chat_id`, `message_count`, `last_posted_at`, and provisional display title
- Order descending by latest message time, then `chat_id`

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_queries_groups.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_queries.py tests/test_web_queries_groups.py
git commit -m "feat: add group list query helpers"
```

### Task 4: Add Message Timeline Query and Serialization

**Files:**
- Create: `src/telegram_kol_research/web_serialization.py`
- Modify: `src/telegram_kol_research/web_queries.py`
- Create: `tests/test_web_queries_messages.py`

**Step 1: Write the failing test**

```python
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset, RawMessage
from telegram_kol_research.web_queries import load_group_messages


def test_load_group_messages_includes_media_and_orders_newest_first(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        older = RawMessage(chat_id=9, message_id=1, posted_at=datetime(2026, 4, 1, tzinfo=UTC), text="older")
        newer = RawMessage(chat_id=9, message_id=2, posted_at=datetime(2026, 4, 2, tzinfo=UTC), text="newer")
        session.add_all([older, newer])
        session.flush()
        session.add(MediaAsset(raw_message_id=newer.id, kind="photo", local_path="data/media/9/2.jpg"))
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)

    assert rows[0]["message_id"] == 2
    assert rows[0]["media_assets"][0]["local_path"] == "data/media/9/2.jpg"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_queries_messages.py -v`
Expected: FAIL because the message query helper does not exist.

**Step 3: Write minimal implementation**

- Implement `load_group_messages(session_factory, chat_id, limit, before_message_id=None)`
- Reuse `dataset_export.py` structure where practical
- Include media asset arrays and reply context
- Order by `posted_at DESC`, fallback `message_id DESC`

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_queries_messages.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_queries.py src/telegram_kol_research/web_serialization.py tests/test_web_queries_messages.py
git commit -m "feat: add message timeline queries"
```

### Task 5: Render the Main Jinja2 Workbench Page

**Files:**
- Create: `src/telegram_kol_research/templates/base.html`
- Create: `src/telegram_kol_research/templates/index.html`
- Create: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/web_app.py`
- Create: `tests/test_web_page_render.py`

**Step 1: Write the failing test**

```python
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.web_app import create_web_app


def test_index_page_shows_group_list_and_messages(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(RawMessage(chat_id=77, message_id=1, posted_at=datetime(2026, 4, 2, tzinfo=UTC), text="hello web"))
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    assert "hello web" in response.text
    assert "77" in response.text
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_page_render.py -v`
Expected: FAIL because the app does not yet render template-based content.

**Step 3: Write minimal implementation**

- Mount templates and static files in the FastAPI app
- Render group list and initial message timeline on `/`
- Build a three-panel layout: group list, messages, AI panel placeholder

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_page_render.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/templates/base.html src/telegram_kol_research/templates/index.html src/telegram_kol_research/static/app.css src/telegram_kol_research/web_app.py tests/test_web_page_render.py
git commit -m "feat: render telegram web workbench"
```

### Task 6: Safely Serve Downloaded Media

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/web_serialization.py`
- Create: `tests/test_web_media_route.py`

**Step 1: Write the failing test**

```python
from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_media_route_serves_downloaded_file(tmp_path):
    media_file = tmp_path / "media" / "77.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"fake-image")

    app = create_web_app(database_path=tmp_path / "research.db", media_root=tmp_path / "media")
    client = TestClient(app)

    response = client.get("/local-media/77.jpg")

    assert response.status_code == 200
    assert response.content == b"fake-image"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_media_route.py -v`
Expected: FAIL because the media-serving route does not exist.

**Step 3: Write minimal implementation**

- Add a static file route rooted at an approved media directory
- Normalize media URLs in the serializer so templates can render image previews safely
- Reject path traversal attempts

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_media_route.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/web_app.py src/telegram_kol_research/web_serialization.py tests/test_web_media_route.py
git commit -m "feat: serve downloaded telegram media safely"
```

### Task 7: Add Grounded AI Scope Builder

**Files:**
- Create: `src/telegram_kol_research/llm_chat.py`
- Create: `tests/test_llm_chat_scope.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.llm_chat import build_scope_context


def test_build_scope_context_includes_message_text_media_and_reply_context():
    messages = [
        {
            "raw_message_id": 10,
            "message_id": 100,
            "sender_name": "Alice",
            "text": "BTC long here",
            "reply_context": {"message_id": 99, "text": "Earlier context"},
            "media_assets": [{"kind": "photo", "ocr_text": "entry 68000"}],
        }
    ]

    context = build_scope_context(messages)

    assert "BTC long here" in context
    assert "Earlier context" in context
    assert "entry 68000" in context
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_chat_scope.py -v`
Expected: FAIL because the scope builder does not exist.

**Step 3: Write minimal implementation**

- Create `build_scope_context(messages)` that renders a bounded textual context block
- Keep source identifiers in the output so later answers can cite them
- Do not call any external API yet

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_llm_chat_scope.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/llm_chat.py tests/test_llm_chat_scope.py
git commit -m "feat: add grounded llm scope builder"
```

### Task 8: Add CLIProxyAPI Request Builder and Chat Endpoint

**Files:**
- Modify: `src/telegram_kol_research/llm_chat.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Create: `tests/test_llm_chat_request.py`
- Create: `tests/test_web_chat_api.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.llm_chat import build_proxy_chat_payload


def test_build_proxy_chat_payload_matches_openai_compatible_shape():
    payload = build_proxy_chat_payload(
        question="Summarize this group",
        scope_context="message context",
        model="gpt-test",
    )

    assert payload["model"] == "gpt-test"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][-1]["content"] == "Summarize this group"
```

```python
from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_chat_api_rejects_missing_question(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.post("/api/chat", json={})

    assert response.status_code == 422
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_chat_request.py tests/test_web_chat_api.py -v`
Expected: FAIL because the payload builder and chat endpoint do not exist.

**Step 3: Write minimal implementation**

- Add environment-backed config loader for CLIProxyAPI settings
- Build an OpenAI-compatible payload
- Add `POST /api/chat` that validates input, loads bounded message scope, and returns a structured JSON response
- For the first passing implementation, allow dependency injection or a fake client in tests

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_llm_chat_request.py tests/test_web_chat_api.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/llm_chat.py src/telegram_kol_research/web_app.py tests/test_llm_chat_request.py tests/test_web_chat_api.py
git commit -m "feat: add grounded chat api via llm proxy"
```

### Task 9: Add Source References to AI Responses

**Files:**
- Modify: `src/telegram_kol_research/llm_chat.py`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Create: `tests/test_llm_chat_references.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.llm_chat import build_source_reference_map


def test_build_source_reference_map_indexes_messages_for_citation_rendering():
    references = build_source_reference_map(
        [{"raw_message_id": 5, "message_id": 50, "sender_name": "Bob", "text": "ETH short"}]
    )

    assert references[0]["raw_message_id"] == 5
    assert references[0]["label"].startswith("[1]")
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_chat_references.py -v`
Expected: FAIL because the source reference helper does not exist.

**Step 3: Write minimal implementation**

- Add a helper that creates citation labels and jump targets from scoped messages
- Update the template and JS to render clickable source references in the AI panel

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_llm_chat_references.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/llm_chat.py src/telegram_kol_research/templates/index.html src/telegram_kol_research/static/app.js tests/test_llm_chat_references.py
git commit -m "feat: add source references to ai responses"
```

### Task 10: Add Server-Sent Events for Live Message Updates

**Files:**
- Create: `src/telegram_kol_research/live_updates.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Create: `tests/test_live_updates.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.live_updates import LiveUpdateBroker


def test_live_update_broker_formats_message_event():
    broker = LiveUpdateBroker()
    payload = broker.format_message_event(chat_id=7, message_id=99)

    assert payload.startswith("event: message")
    assert '"chat_id": 7' in payload
    assert '"message_id": 99' in payload
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_live_updates.py -v`
Expected: FAIL because the broker does not exist.

**Step 3: Write minimal implementation**

- Add an in-process broker that can publish message events
- Add `GET /api/events` SSE route
- Return properly formatted SSE frames for message notifications

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_live_updates.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/live_updates.py src/telegram_kol_research/web_app.py tests/test_live_updates.py
git commit -m "feat: add server-sent events for live updates"
```

### Task 11: Publish Live Events from Message Persistence

**Files:**
- Modify: `src/telegram_kol_research/raw_ingest.py`
- Modify: `src/telegram_kol_research/live_updates.py`
- Create: `tests/test_raw_ingest_live_events.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.live_updates import LiveUpdateBroker
from telegram_kol_research.raw_ingest import NormalizedMessageRecord, persist_normalized_messages


def test_persist_normalized_messages_publishes_inserted_message_events(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    broker = LiveUpdateBroker()

    persist_normalized_messages(
        session_factory,
        [
            NormalizedMessageRecord(
                chat_id=1,
                message_id=10,
                sender_id=None,
                sender_name="Alice",
                text="new",
                reply_to_message_id=None,
                media_kind=None,
                media_path=None,
                media_payload=None,
                archived_target_group=True,
                posted_at=None,
                edit_date=None,
                raw_payload="{}",
            )
        ],
        broker=broker,
    )

    assert broker.published_events[-1]["message_id"] == 10
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_raw_ingest_live_events.py -v`
Expected: FAIL because persistence does not publish broker events.

**Step 3: Write minimal implementation**

- Extend `persist_normalized_messages` with an optional broker dependency
- Publish an event only after successful persistence
- Distinguish inserted versus updated message events if needed, but keep v1 minimal

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_raw_ingest_live_events.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/raw_ingest.py src/telegram_kol_research/live_updates.py tests/test_raw_ingest_live_events.py
git commit -m "feat: publish live update events on message persistence"
```

### Task 12: Add Listener Recovery and Reconciliation Helpers

**Files:**
- Create: `src/telegram_kol_research/reconcile.py`
- Modify: `src/telegram_kol_research/telegram_client.py`
- Create: `tests/test_reconcile.py`

**Step 1: Write the failing test**

```python
from datetime import UTC, datetime

from telegram_kol_research.reconcile import build_reconcile_window


def test_build_reconcile_window_replays_small_safety_window_after_checkpoint():
    start_at, end_at = build_reconcile_window(
        checkpoint_message_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
        now=datetime(2026, 4, 17, 9, 0, tzinfo=UTC),
        safety_minutes=15,
    )

    assert start_at.isoformat().startswith("2026-04-17T07:45")
    assert end_at.isoformat().startswith("2026-04-17T09:00")
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_reconcile.py -v`
Expected: FAIL because the reconcile helper does not exist.

**Step 3: Write minimal implementation**

- Add a helper for checkpoint-based replay windows
- Keep the first helper pure and testable
- Do not wire a background scheduler yet

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_reconcile.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/reconcile.py src/telegram_kol_research/telegram_client.py tests/test_reconcile.py
git commit -m "feat: add reconcile window helpers for listener recovery"
```

### Task 13: Preserve Idempotency for Replayed or Edited Messages

**Files:**
- Modify: `src/telegram_kol_research/raw_ingest.py`
- Create: `tests/test_raw_ingest_idempotency.py`

**Step 1: Write the failing test**

```python
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.raw_ingest import NormalizedMessageRecord, persist_normalized_messages


def test_replaying_same_chat_and_message_id_updates_existing_row_without_duplicates(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first = NormalizedMessageRecord(
        chat_id=1,
        message_id=10,
        sender_id=None,
        sender_name="Alice",
        text="first",
        reply_to_message_id=None,
        media_kind=None,
        media_path=None,
        media_payload=None,
        archived_target_group=True,
        posted_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
        edit_date=None,
        raw_payload="{}",
    )
    edited = NormalizedMessageRecord(
        chat_id=1,
        message_id=10,
        sender_id=None,
        sender_name="Alice",
        text="edited",
        reply_to_message_id=None,
        media_kind=None,
        media_path=None,
        media_payload=None,
        archived_target_group=True,
        posted_at=datetime(2026, 4, 17, 8, 0, tzinfo=UTC),
        edit_date=datetime(2026, 4, 17, 8, 5, tzinfo=UTC),
        raw_payload="{}",
    )

    persist_normalized_messages(session_factory, [first])
    persist_normalized_messages(session_factory, [edited])

    with session_factory() as session:
        rows = session.query(RawMessage).all()

    assert len(rows) == 1
    assert rows[0].text == "edited"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_raw_ingest_idempotency.py -v`
Expected: FAIL if replayed messages create duplicates or edits are not applied correctly.

**Step 3: Write minimal implementation**

- Tighten any persistence logic required to guarantee idempotent replay behavior
- Confirm edit metadata updates existing rows
- Keep the persistence contract safe for reconcile replays

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_raw_ingest_idempotency.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/raw_ingest.py tests/test_raw_ingest_idempotency.py
git commit -m "fix: preserve idempotent message replay behavior"
```

### Task 14: Wire the Frontend AI Panel and Live Timeline Refresh

**Files:**
- Create: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.css`
- Create: `tests/test_web_assets_smoke.py`

**Step 1: Write the failing test**

```python
from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_static_assets_are_served(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/static/app.js")

    assert response.status_code == 200
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_assets_smoke.py -v`
Expected: FAIL because the JS asset is not present or not mounted.

**Step 3: Write minimal implementation**

- Add JS for:
  - group selection-driven fetches if needed
  - AI form submission
  - source reference jump behavior
  - SSE subscription and prepending new message cards
- Keep the first implementation small and readable

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_web_assets_smoke.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/telegram_kol_research/static/app.js src/telegram_kol_research/templates/index.html src/telegram_kol_research/static/app.css tests/test_web_assets_smoke.py
git commit -m "feat: wire ai panel and live timeline updates"
```

### Task 15: Document the Operator Workflow

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Create: `tests/test_readme_web_commands.py`

**Step 1: Write the failing test**

```python
from pathlib import Path


def test_readme_mentions_web_command():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "telegram-kol-research web" in readme
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_readme_web_commands.py -v`
Expected: FAIL because the README does not mention the new web workflow.

**Step 3: Write minimal implementation**

- Document how to launch the web UI
- Document required LLM proxy environment variables
- Document how live updates and reconcile behavior work in operator terms

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_readme_web_commands.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add README.md docs/runbook.md tests/test_readme_web_commands.py
git commit -m "docs: add telegram web workbench workflow"
```

### Task 16: Run Final Verification

**Files:**
- Modify: none
- Test: `tests/test_web_cli.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_web_queries_groups.py`
- Test: `tests/test_web_queries_messages.py`
- Test: `tests/test_web_page_render.py`
- Test: `tests/test_web_media_route.py`
- Test: `tests/test_llm_chat_scope.py`
- Test: `tests/test_llm_chat_request.py`
- Test: `tests/test_web_chat_api.py`
- Test: `tests/test_llm_chat_references.py`
- Test: `tests/test_live_updates.py`
- Test: `tests/test_raw_ingest_live_events.py`
- Test: `tests/test_reconcile.py`
- Test: `tests/test_raw_ingest_idempotency.py`
- Test: `tests/test_web_assets_smoke.py`
- Test: `tests/test_readme_web_commands.py`

**Step 1: Run the focused web and live-update tests**

Run:

```bash
pytest tests/test_web_cli.py tests/test_web_app.py tests/test_web_queries_groups.py tests/test_web_queries_messages.py tests/test_web_page_render.py tests/test_web_media_route.py tests/test_llm_chat_scope.py tests/test_llm_chat_request.py tests/test_web_chat_api.py tests/test_llm_chat_references.py tests/test_live_updates.py tests/test_raw_ingest_live_events.py tests/test_reconcile.py tests/test_raw_ingest_idempotency.py tests/test_web_assets_smoke.py tests/test_readme_web_commands.py -v
```

Expected: PASS

**Step 2: Run the broader test suite**

Run: `pytest tests -v`
Expected: PASS, or only clearly pre-existing unrelated failures.

**Step 3: Smoke test the web command manually**

Run:

```bash
PYTHONPATH=src python -m telegram_kol_research.cli web --help
```

Expected: exit code 0 and printed usage instructions.

**Step 4: Commit**

```bash
git add .
git commit -m "feat: add telegram web workbench with grounded ai chat"
```
