# Phase 6 Ingest Refresh RPC Implementation Plan

**Goal:** Preserve `POST /api/refresh` while ensuring only the ingest runtime role can open the Telethon session.

**Architecture:** Add one default-`all` runtime-role selector shared by CLI and app lifespan. In split mode, Web forwards one bounded localhost request to ingest; ingest executes the unchanged refresh implementation; worker rejects it. No retry, schema, migration, exchange semantic, or recognition semantic change is permitted.

**Tech stack:** Python, Typer, FastAPI, httpx, pytest, systemd.

---

### Task 1: Characterize the approved role and refresh contract

**Files:**
- Create: `tests/test_runtime_role_selection.py`
- Inspect only: `src/telegram_kol_research/web_app.py`
- Inspect only: `src/telegram_kol_research/cli.py`

1. Add tests asserting valid roles are `all`, `ingest`, `worker`, and `web`, with
   invalid roles rejected before startup.
2. Add a test proving the default remains `all` and the existing local refresh
   implementation is called exactly once.
3. Run the focused tests and verify RED because role selection does not exist.

### Task 2: Make Telegram startup role-owned

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_runtime_role_selection.py`
- Test: `tests/test_web_cli.py`

1. Add failing CLI tests proving `web` and `worker` never load Telegram auth,
   acquire the session lock, or construct the client, while `all` and `ingest`
   retain the current order.
2. Run the focused tests and verify the expected RED.
3. Add the minimal `--runtime-role` option, default `all`, pass it to
   `create_web_app`, and gate Telegram startup to `all`/`ingest`.
4. Run the focused tests and verify GREEN.
5. Commit only the exact code/test paths.

### Task 3: Add the bounded Web-to-ingest refresh proxy

**Files:**
- Modify: `src/telegram_kol_research/web_app.py`
- Test: `tests/test_runtime_role_selection.py`

1. Add failing tests for one-call success passthrough, non-2xx status/body
   passthrough, no retry after transport failure, and worker rejection.
2. Run the focused tests and verify the expected RED.
3. Extract the unchanged local refresh body and add role dispatch. Use one
   injected async requester with the existing refresh timeout; never retry.
4. Run the focused tests and the affected Web/CLI session-lock tests; verify
   GREEN.
5. Commit only the exact code/test paths.

### Task 4: Re-run Phase 6 Task 1 gates

**Files:**
- Modify only if evidence requires: `docs/runtime-serialization-remediation-status.md`

1. Run `tests/test_process_boundary_authority.py`, the new role tests, and the
   existing Telegram session-lock/CLI tests.
2. Statically verify no Web/worker role path loads auth or opens the session.
3. If any gate fails, release the claim, record evidence, and stop.
4. If all gates pass, continue at canonical Phase 6 Task 2 without rereading any
   other phase file.
