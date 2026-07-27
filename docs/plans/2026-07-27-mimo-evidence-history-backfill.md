# MiMo Evidence History Backfill Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Backfill immutable first-pass MiMo text/image evidence for bounded historical messages without running DeepSeek, strategy application, or exchange actions.

**Architecture:** Add a standalone evidence-only batch service that selects messages from explicit chat/time bounds, skips matching completed evidence by input fingerprint, and persists each MiMo result independently. Expose it through a fail-closed Typer command with dry-run by default, bounded retry opt-in, stable ordering, and rate limiting.

**Tech Stack:** Python 3.12+, SQLAlchemy, SQLite, Typer, existing MiMo OpenAI-compatible API, pytest.

---

Implementation must follow @test-driven-development. Before deployment, use
@requesting-code-review and keep contextual live resolution disabled.

### Task 1: Implement evidence-only historical selection and execution

**Files:**
- Create: `src/telegram_kol_research/evidence_backfill.py`
- Create: `tests/test_evidence_backfill.py`

**Step 1: Write failing selection tests**

Test that the planner:

- requires at least one explicit chat ID;
- orders messages oldest-first;
- respects start/end/limit;
- returns `process` for missing or changed evidence;
- returns `skip_completed` for a matching completed fingerprint;
- returns `skip_failed` for a matching failed fingerprint unless retry is enabled;
- skips messages with no text and no media.

**Step 2: Run tests to verify RED**

Run:

```bash
uv run --frozen pytest tests/test_evidence_backfill.py -q
```

Expected: collection fails because `evidence_backfill` does not exist.

**Step 3: Implement the minimal planner**

Add immutable plan/result dataclasses and:

```python
def plan_mimo_evidence_backfill(
    session_factory,
    *,
    chat_ids: Sequence[int],
    media_root: str | Path,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = 100,
    retry_failed: bool = False,
) -> EvidenceBackfillPlan: ...
```

Use the current evidence row plus `build_message_input_fingerprint`. Validate non-empty
chat scope and positive limit.

**Step 4: Verify planner GREEN**

Run the focused test file and expect PASS.

**Step 5: Write failing execution tests**

Inject a MiMo runner and sleeper. Assert:

- dry-run performs no model call and no evidence write;
- apply calls MiMo once per `process` item and persists source-separated evidence;
- a failure is persisted and processing continues;
- per-message persistence makes a second run resumable;
- execution does not accept a context resolver or trade executor.

**Step 6: Implement the minimal executor**

Add:

```python
def run_mimo_evidence_backfill(
    session_factory,
    *,
    plan: EvidenceBackfillPlan,
    ai_recognition_config,
    media_root: str | Path,
    apply: bool,
    delay_seconds: float,
    mimo_runner=run_mimo_authoritative_for_message,
    sleeper=time.sleep,
) -> EvidenceBackfillResult: ...
```

Only call `run_mimo_authoritative_for_message` and
`persist_mimo_message_evidence`. Persist each result before continuing.

**Step 7: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_evidence_backfill.py -q
```

Expected: PASS.

**Step 8: Commit**

```bash
git add src/telegram_kol_research/evidence_backfill.py tests/test_evidence_backfill.py
git commit -m "feat: backfill historical MiMo evidence"
```

### Task 2: Add the fail-closed CLI

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_cli_evidence_backfill.py`

**Step 1: Write failing CLI tests**

Use `CliRunner` to assert:

- no chat scope exits non-zero without loading MiMo;
- repeated `--chat-id` values are normalized;
- `--use-configured-context-chats` reads the persisted list even while live resolution
  is disabled;
- default output is dry-run;
- `--apply` passes limits, retry behavior, time bounds, and delay to the service;
- output contains aggregate statuses but no raw payload.

**Step 2: Verify RED**

Run:

```bash
uv run --frozen pytest tests/test_cli_evidence_backfill.py -q
```

Expected: FAIL because the command does not exist.

**Step 3: Implement the CLI**

Add `backfill-mimo-evidence`. Parse ISO-8601 timestamps as UTC-naive database values,
merge/dedupe explicit and configured chat IDs, reject an empty scope, load AI config
only for `--apply`, then print bounded JSON statistics.

**Step 4: Verify GREEN**

Run the CLI and service tests; expect PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/cli.py tests/test_cli_evidence_backfill.py
git commit -m "feat: expose MiMo evidence backfill command"
```

### Task 3: Document, review, and verify

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `docs/contextual-strategy-resolution.md`

**Step 1: Document safe operations**

Add dry-run, small-batch apply, resume, failed retry, audit queries, and explicit
warnings that the command does not execute strategy decisions.

**Step 2: Run focused and regression tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_evidence_backfill.py \
  tests/test_cli_evidence_backfill.py \
  tests/test_message_evidence.py \
  tests/test_authoritative_recognition.py \
  tests/test_context_resolution_replay.py -q
uv run --frozen python -m compileall -q src tests
git diff --check
```

Expected: PASS.

**Step 3: Request code review**

Review for evidence-only boundaries, idempotency, retry bounds, sensitive output, and
production safety. Resolve all Critical and Important findings.

**Step 4: Commit documentation and fixes**

```bash
git add README.md docs/runbook.md docs/contextual-strategy-resolution.md
git commit -m "docs: operate historical MiMo evidence backfill"
```

### Task 4: Integrate, deploy disabled, and run bounded production backfill

**Files:**
- Review all commits in the isolated worktree.

**Step 1: Re-run the deployment safety suite**

Run the focused tests from the contextual strategy plan plus the new backfill tests.

**Step 2: Integrate onto the required branch**

Cherry-pick or merge the reviewed commits onto `codex/deepcoin-auto-trading-v1`, then
push that branch.

**Step 3: Deploy**

Use:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Verify the service remains active and contextual live resolution remains disabled.

**Step 4: Dry-run one allowlisted group**

Run:

```bash
telegram-kol-research backfill-mimo-evidence \
  --database-path data/research.db \
  --chat-id=-1002805019371 \
  --limit 25
```

Review only counts and IDs.

**Step 5: Apply a bounded batch**

Repeat with `--apply --delay-seconds 2`. Verify evidence versions, source separation,
failure status, no strategy/thread/exchange mutations, and service health.

**Step 6: Continue bounded batches**

Increase the limit only after the first batch is verified. Use `--retry-failed` only
for reviewed transient failures.
