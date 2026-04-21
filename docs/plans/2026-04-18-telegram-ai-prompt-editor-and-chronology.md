# Telegram AI Prompt Editor and Chronology Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-group editable default prompts, cleaner AI chat presentation, and chronology-aware grounding so the Telegram AI panel behaves more like a practical research copilot.

**Architecture:** Extend the existing FastAPI + Jinja2 + vanilla JavaScript stack. Keep prompt editing client-side for now using per-group localStorage, pass the saved prompt with each chat request, and reorder scoped message context oldest-to-newest before building the prompt sent to the proxy.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, SQLAlchemy, httpx, vanilla JavaScript, CSS, Pytest

---

### Task 1: Add Prompt Editor Rendering for the Current Group

**Files:**
- Modify: `src/telegram_kol_research/templates/index.html`
- Modify: `tests/test_web_page_render.py`

**Step 1: Write the failing test**

Add assertions that the AI panel renders:

- a prompt editor label or heading
- a textarea for the group prompt
- helper copy indicating it affects only the current group and takes effect on the next question

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_page_render.py -v`
Expected: FAIL because the prompt editor is not rendered yet.

**Step 3: Write minimal implementation**

- Add a prompt-editor section above conversation history in `index.html`
- Keep it lightweight and server-rendered

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_web_page_render.py -v`
Expected: PASS

### Task 2: Add Per-Group Prompt Storage and Use It on the Next Question

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/templates/index.html`

**Step 1: Write the failing test**

If browserless coverage is not practical, define a narrow manual verification checklist and keep implementation deterministic.

Required behaviors:

- Prompt key uses current `chat_id`
- Switching groups reloads that group's saved prompt
- Saving a prompt does not require a page reload
- Next submitted chat request includes the saved prompt text

**Step 2: Run the relevant focused tests**

Run the nearest affected tests.
Expected: No coverage yet or indirect failures requiring implementation.

**Step 3: Write minimal implementation**

- Add `getPromptKey`, `loadGroupPrompt`, and `saveGroupPrompt` helpers
- Auto-load the current group's prompt into the prompt editor
- Include the saved prompt in the `/api/chat` request payload as `group_prompt`

**Step 4: Run focused verification**

Re-run affected tests and manual verification checklist.
Expected: PASS

### Task 3: Extend Chat Payload Builder to Include Editable Group Prompt

**Files:**
- Modify: `src/telegram_kol_research/llm_chat.py`
- Modify: `tests/test_llm_chat_request.py`

**Step 1: Write the failing test**

Add assertions that `build_proxy_chat_payload(...)` accepts and includes the user-editable prompt instruction layer.

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_llm_chat_request.py -v`
Expected: FAIL because the payload builder does not support `group_prompt` yet.

**Step 3: Write minimal implementation**

- Add an optional `group_prompt` parameter to `build_proxy_chat_payload`
- Inject it as a distinct message or system-layer instruction before user question content

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_llm_chat_request.py -v`
Expected: PASS

### Task 4: Reorder AI Scope Context Chronologically

**Files:**
- Modify: `src/telegram_kol_research/llm_chat.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_llm_chat_scope.py`
- Modify: `tests/test_web_chat_api.py`

**Step 1: Write the failing test**

Add assertions that scoped messages are passed to the model oldest-to-newest within the selected recent slice.

For example:

- database returns latest-first for UI
- prompt context should show older message before newer message

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_llm_chat_scope.py tests/test_web_chat_api.py -v`
Expected: FAIL because current context follows latest-first ordering.

**Step 3: Write minimal implementation**

- Reverse the loaded message slice before building scope context
- Add an explicit prompt instruction that later entries are newer and should be weighted more heavily for recent-state analysis

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_llm_chat_scope.py tests/test_web_chat_api.py -v`
Expected: PASS

### Task 5: Clean Up AI History Labels and Error Messaging

**Files:**
- Modify: `src/telegram_kol_research/static/app.js`
- Modify: `src/telegram_kol_research/static/app.css`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_chat_api.py`

**Step 1: Write the failing test**

Add assertions for the preferred image-capability error wording.

If practical, add render assertions that the history markup no longer uses noisy `You` / `AI` label text.

**Step 2: Run test to verify it fails**

Run the relevant focused tests.
Expected: FAIL because the old wording/labels remain.

**Step 3: Write minimal implementation**

- Replace noisy history labels with subtler wording or remove them entirely
- Update the image-capability error detail to the cleaner Chinese guidance

**Step 4: Run test to verify it passes**

Run the relevant focused tests.
Expected: PASS

### Task 6: Update Docs and Re-Verify

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook.md`

**Step 1: Update docs**

- Mention per-group default prompt editing
- Mention next-question immediate effect
- Mention chronology-aware recent-message analysis

**Step 2: Run verification**

Run:

```bash
PYTHONPATH=src .venv313b/bin/python -m pytest tests/test_readme_commands.py tests/test_readme_web_commands.py -v
PYTHONPATH=src .venv313b/bin/python -m pytest
```

Expected: PASS
