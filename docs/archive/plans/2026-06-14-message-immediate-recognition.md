# Message Immediate Recognition Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-message manual recognition and Web navigation for prompt/config screens.

**Architecture:** Keep the existing FastAPI + Jinja2 + vanilla JavaScript stack. Add a small recognition service around the current text parser, expose it through a POST API, and refresh the current message panel after a recognition action. Store the recognition prompt in a YAML config file for later LLM/OCR integration.

**Tech Stack:** Python, FastAPI, SQLAlchemy, Jinja2, vanilla JavaScript, pytest.

---

### Task 1: Prompt Config Storage

**Files:**
- Create: `src/telegram_kol_research/ai_recognition_config.py`
- Create: `config/ai_recognition.example.yaml`
- Test: `tests/test_ai_recognition_config.py`

**Steps:**
1. Add tests for default prompt loading, missing file fallback, and saving a custom prompt.
2. Implement YAML-backed `load_ai_recognition_config()` and `save_ai_recognition_config()`.
3. Run `PYTHONPATH=src .venv/bin/python -m pytest tests/test_ai_recognition_config.py -q`.

### Task 2: Immediate Recognition Service

**Files:**
- Create: `src/telegram_kol_research/message_recognition.py`
- Test: `tests/test_message_recognition.py`

**Steps:**
1. Add tests for text strategy recognition, non-strategy text, video skip, image pending, and missing message errors.
2. Implement `recognize_message_now(session_factory, raw_message_id)` using `parse_signal_text()`.
3. Upsert one `SignalCandidate` per recognized message.
4. Run `PYTHONPATH=src .venv/bin/python -m pytest tests/test_message_recognition.py -q`.

### Task 3: Web API And Message Button

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/_messages.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Test: `tests/test_web_group_messages_route.py`
- Test: `tests/test_web_app.py`

**Steps:**
1. Add route tests for `data-recognize-message` buttons and `POST /api/messages/{raw_message_id}/recognize`.
2. Add a button on each message card.
3. Add JS click handler to disable the button, call the API, and refresh the message panel.
4. Add compact CSS for the action/status.
5. Run the relevant Web tests.

### Task 4: Top Navigation Screens

**Files:**
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_web_page_render.py`

**Steps:**
1. Add tests for menu labels and prompt/config panels.
2. Render menu tabs below the title.
3. Add prompt editor form backed by config APIs.
4. Add AI config panel showing V1 local parser mode.
5. Add JS tab switching and prompt save handler.
6. Verify in browser at `http://127.0.0.1:8000/`.
