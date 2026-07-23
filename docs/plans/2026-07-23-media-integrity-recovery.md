# Media Integrity Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent corrupt Telegram image downloads from silently bypassing strategy recognition and make them eligible for safe recovery.

**Architecture:** Define a shared media-usability predicate for non-empty, decodable images. The downloader promotes only validated output, while the listener replays messages whose media is missing, empty, or corrupt. Historical recovery remains evidence-only and never invokes the trading executor.

**Tech Stack:** Python 3.12, Telethon, Pillow, SQLAlchemy, pytest.

---

### Task 1: Add a shared image-media usability predicate

**Files:**
- Modify: `src/telegram_kol_research/telegram_client.py`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Test: `tests/test_telegram_fetch.py`

**Step 1: Write the failing test**

Add tests showing a zero-byte file and corrupt image bytes are unusable, while a valid JPEG is usable.

**Step 2: Run the test to verify failure**

Run: `uv run pytest tests/test_telegram_fetch.py -q`

**Step 3: Write minimal implementation**

Implement one helper that checks regular-file, positive-size, and Pillow image verification. Reuse it in recognition so empty and corrupt images receive distinct reasons.

**Step 4: Run the test to verify pass**

Run: `uv run pytest tests/test_telegram_fetch.py tests/test_recognition_experiments.py -q`

**Step 5: Commit**

Run: `git add src/telegram_kol_research/telegram_client.py src/telegram_kol_research/message_recognition.py tests/test_telegram_fetch.py && git commit -m "fix: validate Telegram image media"`

### Task 2: Make completed downloads atomic and reject invalid output

**Files:**
- Modify: `src/telegram_kol_research/telegram_client.py`
- Test: `tests/test_telegram_fetch.py`

**Step 1: Write the failing test**

Add a fake client that leaves a zero-byte temporary output and assert no media path is returned. Add a fake client that writes a valid JPEG and assert promotion to a canonical path without a duplicate suffix.

**Step 2: Run the test to verify failure**

Run: `uv run pytest tests/test_telegram_fetch.py -q`

**Step 3: Write minimal implementation**

Download to a per-message temporary filename. Validate its returned output, remove invalid temporary artifacts, and atomically replace the canonical file only after validation.

**Step 4: Run the test to verify pass**

Run: `uv run pytest tests/test_telegram_fetch.py -q`

**Step 5: Commit**

Run: `git add src/telegram_kol_research/telegram_client.py tests/test_telegram_fetch.py && git commit -m "fix: atomically persist Telegram media"`

### Task 3: Replay all unusable image rows safely

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Test: `tests/test_telegram_live_listener.py`

**Step 1: Write the failing test**

Add a stored media path pointing to a zero-byte file and assert its message ID is included in the listener's recovery fetch set.

**Step 2: Run the test to verify failure**

Run: `uv run pytest tests/test_telegram_live_listener.py -q`

**Step 3: Write minimal implementation**

Select rows in the existing bounded replay window and use the shared predicate to build the retry set. Keep recovered historical rows out of the auto-trade executor.

**Step 4: Run the test to verify pass**

Run: `uv run pytest tests/test_telegram_live_listener.py tests/test_telegram_fetch.py -q`

**Step 5: Commit**

Run: `git add src/telegram_kol_research/telegram_live_listener.py tests/test_telegram_live_listener.py && git commit -m "fix: replay unusable Telegram media"`

### Task 4: Validate and deploy

**Files:**
- Verify: `tests/test_telegram_fetch.py`
- Verify: `tests/test_telegram_live_listener.py`
- Verify: `tests/test_recognition_experiments.py`

**Step 1: Run focused tests**

Run: `uv run pytest tests/test_telegram_fetch.py tests/test_telegram_live_listener.py tests/test_recognition_experiments.py -q`

**Step 2: Request code review**

Check the final diff for path traversal, deletion of valid media, and any route from historical recovery to auto-trading.

**Step 3: Push and deploy**

Run: `git push origin codex/deepcoin-auto-trading-v1 && ./scripts/server_git_update.sh`

**Step 4: Production verification**

Run a read-only scan for non-null media paths that are empty or undecodable, verify service health, and confirm recovery created no Deepcoin order.
