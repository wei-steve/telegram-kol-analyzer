# Telegram AI Panel Simplification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Simplify the Telegram web workbench AI panel so it defaults to analyzing the current group's recent messages without manual scope controls, while improving multi-turn conversation readability.

**Architecture:** Keep the existing FastAPI + Jinja2 + light JavaScript architecture. Remove explicit AI scope controls from the UI, add a small backend helper that extracts recent-message count overrides from question text, and restructure the client-side conversation history into clearer turn-based rendering.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, SQLAlchemy, httpx, vanilla JavaScript, CSS, Pytest

---

### Task 1: Add a Question Parser for Recent Message Count Overrides

**Files:**
- Modify: `src/telegram_kol_research/llm_chat.py`
- Create: `tests/test_llm_chat_scope.py`

**Step 1: Write the failing test**

```python
from telegram_kol_research.llm_chat import extract_recent_message_limit


def test_extract_recent_message_limit_defaults_to_none_when_not_requested():
    assert extract_recent_message_limit("总结这个群最近在讨论什么") is None


def test_extract_recent_message_limit_reads_chinese_recent_count_patterns():
    assert extract_recent_message_limit("总结最近100条消息") == 100
    assert extract_recent_message_limit("分析最近 200 条里最重要的观点") == 200
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_llm_chat_scope.py -v`
Expected: FAIL because `extract_recent_message_limit` does not exist yet.

**Step 3: Write minimal implementation**

- Add `extract_recent_message_limit(question: str) -> int | None` in `llm_chat.py`
- Support narrow recent-count patterns such as:
  - `最近100条`
  - `最近 100 条`
  - `recent 100 messages`
- Return `None` when no explicit count is requested

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_llm_chat_scope.py -v`
Expected: PASS

### Task 2: Simplify the Chat API to Default to Current Group Recent Messages

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/llm_chat.py`
- Modify: `tests/test_web_chat_api.py`

**Step 1: Write the failing test**

```python
def test_chat_api_defaults_to_latest_50_messages_for_current_group(tmp_path):
    ...


def test_chat_api_uses_question_requested_recent_message_limit(tmp_path):
    ...
```

Key assertions:

- When question text does not request a count, `load_group_messages(..., limit=50)` is used
- When question text includes `最近100条`, `load_group_messages(..., limit=100)` is used
- The request no longer depends on `scope_mode`, `selected_message_ids`, or time-window inputs for the primary path

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_chat_api.py -v`
Expected: FAIL because the endpoint still uses explicit scope controls.

**Step 3: Write minimal implementation**

- In `web_app.py`, remove primary-path reliance on `scope_mode`
- Compute `message_limit` from the question via `extract_recent_message_limit`, defaulting to 50
- Load current-group messages with that limit
- Keep current proxy error handling behavior intact

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_chat_api.py -v`
Expected: PASS

### Task 3: Remove Message Selection Checkboxes from the Timeline

**Files:**
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `tests/test_web_group_messages_route.py`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write the failing test**

Add assertions that rendered HTML no longer includes:

- `data-message-select`
- `Select`

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_group_messages_route.py tests/test_web_page_render.py -v`
Expected: FAIL because the current template still renders selection checkboxes.

**Step 3: Write minimal implementation**

- Remove checkbox markup from `_messages.html`
- Keep sender name, message id, text, media preview, OCR block, filters, and load-more button

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_group_messages_route.py tests/test_web_page_render.py -v`
Expected: PASS

### Task 4: Simplify the AI Panel Template to a Single-Input Flow

**Files:**
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write the failing test**

Add assertions that the main page:

- Does not render `Scope`
- Does not render `Posted after`
- Does render a helper text indicating the default current-group recent-message behavior

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_page_render.py -v`
Expected: FAIL because the current template still renders scope and date controls.

**Step 3: Write minimal implementation**

- Remove the scope select block from `index.html`
- Remove the time-window fields block
- Keep hidden `chat_id`
- Keep one text input or textarea plus submit button
- Add short helper text such as: `默认分析当前群最近 50 条消息；你也可以直接问“总结最近 200 条”。`

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_page_render.py -v`
Expected: PASS

### Task 5: Restructure Conversation History Rendering into Clear Turns

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/templates/index.html`

**Step 1: Write the failing test**

If adding browserless coverage is practical, add a test for rendered history markup shape or at minimum update existing render assertions to expect turn-group containers.

If template-level tests are insufficient, document this task with a focused manual verification checklist and keep JS changes minimal and deterministic.

**Step 2: Run test to verify it fails**

Run the relevant focused tests.
Expected: FAIL or missing expected structure.

**Step 3: Write minimal implementation**

- Change conversation storage from flat role/content entries to grouped turn records
- Render each turn as:
  - user question block
  - assistant answer block
  - optional sources section
- Keep citations clickable in the latest answer area and history where practical
- Preserve per-group localStorage separation

**Step 4: Run test to verify it passes**

Run the relevant focused tests.
Expected: PASS

### Task 6: Update Frontend Chat Submission Logic for the Simplified Flow

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `tests/test_web_chat_api.py`

**Step 1: Write the failing test**

Extend API tests or page-render tests to reflect the simplified request contract by removing now-obsolete UI-driven fields from the expected flow.

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_chat_api.py tests/test_web_page_render.py -v`
Expected: FAIL because the JS/template/API assumptions still reference removed controls.

**Step 3: Write minimal implementation**

- Stop collecting selected-message ids
- Stop reading scope mode and time-window inputs
- Send only `question` and `chat_id` from the primary UI flow
- Preserve error display and answer/source rendering

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_chat_api.py tests/test_web_page_render.py -v`
Expected: PASS

### Task 7: Update Docs and Re-Verify the Whole Workbench

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Run: `tests/test_readme_commands.py`
- Run: `tests/test_readme_web_commands.py`

**Step 1: Write the failing test**

If necessary, add or extend documentation assertions to mention the simplified AI panel behavior.

**Step 2: Run test to verify it fails**

Run the relevant README tests.
Expected: FAIL if new assertions were added.

**Step 3: Write minimal implementation**

- Document that the AI panel now defaults to the current group's recent 50 messages
- Mention that users can override count in natural language, for example `最近 200 条`
- Remove references to manual scope selection if they remain in user-facing docs

**Step 4: Run test to verify it passes**

Run:

```bash
PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_readme_commands.py tests/test_readme_web_commands.py -v
PYTHONPATH=src .venv313b/bin/python -m pytest
```

Expected: PASS
