# MiMo v2 Isolated Replay Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a bounded, fail-closed server command that compares MiMo v1 and v2 against a temporary copy of real message context and media without writing production state or entering any execution path.

**Architecture:** Open the source SQLite database read-only only long enough to create an online-backup working copy in a private temporary directory. Run existing v1 inference, strict v2 inference, and the pure v2 adapter only against that disposable copy; retain only redacted comparison/performance artifacts and delete the working database on every exit.

**Tech Stack:** Python 3.12, SQLite online backup, SQLAlchemy, Typer, dataclasses, JSON/CSV, pytest, existing MiMo recognition and v2 adapter modules.

---

Design reference: `docs/plans/2026-08-11-mimo-v2-isolated-replay-design.md`

Implementation constraints:

- Production must remain on `mimo_contract_mode=v1`.
- Do not push, deploy, restart, or run the command on the server in this task.
- Never create a session factory for the source database path.
- Never call `process_authoritative_message`, auto-trade code, a Deepcoin writer,
  a listener, or a notifier.
- The retained artifacts must contain no source text, image bytes, prompt text,
  raw provider response, credential, or authorization value.
- Tests must observe RED before each production-code increment.

### Task 1: Implement bounded input parsing and a read-only snapshot boundary

**Files:**
- Create: `src/telegram_kol_research/mimo_v2_replay.py`
- Create: `tests/test_mimo_v2_replay.py`

**Step 1: Write failing ID and path-boundary tests**

Add tests for positive IDs, comments, stable duplicate removal, malformed IDs,
empty lists, the 200-message hard limit, missing/symlinked source files,
missing/symlinked media roots, and non-empty/symlinked artifact directories.

```python
def test_load_replay_message_ids_is_bounded_and_stable(tmp_path):
    source = tmp_path / "ids.txt"
    source.write_text("7\n# incident\n9\n7\n", encoding="utf-8")

    assert load_replay_message_ids(source, max_messages=2) == (7, 9)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1 2"])
def test_load_replay_message_ids_rejects_malformed_values(tmp_path, value):
    source = tmp_path / "ids.txt"
    source.write_text(value, encoding="utf-8")

    with pytest.raises(MimoV2ReplayInputError):
        load_replay_message_ids(source, max_messages=200)
```

**Step 2: Run the input tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py -k 'message_ids or path_boundary'
```

Expected: FAIL because `telegram_kol_research.mimo_v2_replay` does not exist.

**Step 3: Add the minimal input types and validators**

Create:

```python
MAX_REPLAY_MESSAGES = 200


class MimoV2ReplayInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MimoV2ReplayInputs:
    source_database: Path
    message_id_file: Path
    media_root: Path
    artifact_dir: Path
    raw_message_ids: tuple[int, ...]


def load_replay_message_ids(path: str | Path, *, max_messages: int) -> tuple[int, ...]:
    # Reject bool-like/non-decimal/zero/negative IDs, preserve first occurrence,
    # and stop before returning more than min(max_messages, 200).
    ...


def validate_replay_inputs(...) -> MimoV2ReplayInputs:
    # Resolve without following symlink boundaries, require regular source/ID
    # files and a real media directory, then create/chmod one new or empty
    # artifact directory to 0700.
    ...
```

Do not create or open a SQLAlchemy engine in these validators.

**Step 4: Run the input tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 5: Write the failing read-only online-backup test**

Seed a database, explicitly dispose its bootstrap engine, capture the main/WAL/
SHM bytes plus size/mtime/mode, and call a snapshot function. Assert that the
working copy contains the selected message and the source signatures are
unchanged.

```python
def test_online_snapshot_reads_source_without_modifying_it(tmp_path):
    source, message_id = seed_source_database(tmp_path)
    before = source_component_signatures(source)
    working = tmp_path / "private" / "working.db"

    create_read_only_replay_snapshot(source, working)

    assert source_component_signatures(source) == before
    with sqlite3.connect(working) as connection:
        assert connection.execute(
            "SELECT id FROM raw_messages WHERE id = ?", (message_id,)
        ).fetchone() == (message_id,)
```

**Step 6: Run the snapshot test and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py -k online_snapshot
```

Expected: FAIL because `create_read_only_replay_snapshot` is missing.

**Step 7: Implement the read-only snapshot**

Implement `create_read_only_replay_snapshot(source_database, destination)` with:

```python
source_uri = source_database.resolve(strict=True).as_uri() + "?mode=ro"
with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
    source.execute("PRAGMA query_only = ON")
    with sqlite3.connect(destination) as target:
        source.backup(target, pages=1024, sleep=0.01)
        target.commit()
```

Require a nonexistent destination inside an already-private temporary
directory. Do not call `PRAGMA journal_mode` on the source. Wrap only bounded
SQLite/OSError failures as `MimoV2ReplayInputError` without including URI query
values.

**Step 8: Run Task 1 tests and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mimo_v2_replay.py
git diff --check
```

Expected: PASS and no whitespace errors.

Commit:

```bash
git add src/telegram_kol_research/mimo_v2_replay.py \
  tests/test_mimo_v2_replay.py
git commit -m "feat: isolate mimo replay inputs"
```

### Task 2: Add conservative projection comparison and deterministic gates

**Files:**
- Modify: `src/telegram_kol_research/mimo_v2_replay.py`
- Modify: `tests/test_mimo_v2_replay.py`

**Step 1: Write failing projection-comparison tests**

Test:

- identical entry, management, exit, cancel, revision, and multi-action
  compatibility projections;
- v1 action omitted by v2;
- new v2 executable action;
- symbol, side, target, entry, stop, take-profit, leverage, order type,
  management fraction, action order, and confidence drift;
- partial/full exit and cancel/close confusion;
- both sides non-executable with different summary wording; and
- reason punctuation never changing the comparison.

```python
def test_execution_field_drift_is_unsafe():
    v1 = compatibility_payload(stop_loss="1940")
    v2 = compatibility_payload(stop_loss="1950")

    comparison = compare_execution_projections(v1, v2)

    assert comparison.status == "unsafe_mismatch"
    assert comparison.reason_code == "execution_projection_mismatch"


def test_non_executable_wording_difference_is_safe():
    comparison = compare_execution_projections(
        no_action_payload(reason="one"),
        no_action_payload(reason="two"),
    )

    assert comparison.status == "safe_match"
    assert comparison.reason_code == "both_non_executable"
```

**Step 2: Run comparison tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py -k 'projection or mismatch or non_executable'
```

Expected: FAIL because the comparison functions are missing.

**Step 3: Implement a closed replay projection**

Add immutable `ReplayProjectionComparison` and pure helpers. Canonicalize only:

- top-level `recognition_result` and confidence when executable;
- `strategy` closed trade fields;
- ordered `instructions` with reason removed but confidence retained;
- `lifecycle_event` with reason and internal authorization markers removed;
- `entry_context` and ordered `entry_fragments` with reason removed.

Use canonical JSON plus SHA-256 fingerprints. Determine executable presence
from entry strategy, non-`none` lifecycle event, or non-empty supported
instructions. If both projections have no executable action, return a safe
match without comparing summaries, reasons, observed text, or image evidence.
Otherwise require exact canonical equality and fail closed.

Do not import a parser, coordinator, candidate projector, or executor.

**Step 4: Run comparison tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 5: Write failing percentile and performance-gate tests**

Test deterministic nearest-rank P95, empty samples, adapter exactly/beyond 50
ms, v2 exactly/beyond 115% of v1, and no successful comparable pairs.

```python
def test_performance_gate_fails_when_v2_exceeds_ratio():
    gate = evaluate_replay_performance(
        v1_duration_ms=[100, 100],
        v2_duration_ms=[116, 116],
        adapter_duration_ms=[1, 1],
    )
    assert gate.passed is False
    assert "v2_latency_ratio_exceeded" in gate.failure_codes
```

**Step 6: Run performance tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py -k 'percentile or performance_gate'
```

Expected: FAIL because the performance helpers are missing.

**Step 7: Implement deterministic performance evaluation**

Add `ReplayPerformanceGate`, `nearest_rank_percentile`, and
`evaluate_replay_performance`. Use `ceil(0.95 * count) - 1`, reject negative or
non-finite timings, require at least one comparable pair, require adapter P95
strictly below `50.0`, and require `v2_p95 <= v1_p95 * 1.15`.

**Step 8: Run Task 2 tests and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mimo_v2_replay.py
git diff --check
```

Expected: PASS.

Commit:

```bash
git add src/telegram_kol_research/mimo_v2_replay.py \
  tests/test_mimo_v2_replay.py
git commit -m "feat: compare mimo replay projections"
```

### Task 3: Run v1 and v2 only inside the disposable working copy

**Files:**
- Modify: `src/telegram_kol_research/mimo_v2_replay.py`
- Modify: `tests/test_mimo_v2_replay.py`

**Step 1: Write failing isolated-runner safety tests**

Inject fake v1/v2 runners and a fake clock. Prove:

- the runner receives a session factory bound to the temporary working copy,
  never the source path;
- v1 and v2 receive the same precomputed context and media root;
- v1-side writes appear only in the copy;
- the private working database is deleted on success and exception;
- missing requested messages fail before the first model call;
- processing is ordered and bounded; and
- the result reports `production_writes=0`, `notifications_sent=0`, and
  `execution_calls=0` as construction invariants.

```python
def test_replay_runners_write_only_disposable_copy(tmp_path):
    seen_database_paths = []

    def fake_v1(session_factory, **kwargs):
        seen_database_paths.append(Path(session_factory.kw["bind"].url.database))
        write_marker(session_factory)
        return successful_v1()

    result = run_mimo_v2_replay(..., v1_runner=fake_v1, v2_runner=fake_v2)

    assert all(path != source_database for path in seen_database_paths)
    assert marker_absent_from_source(source_database)
    assert result.production_writes == 0
    assert no_private_database_remains(result.artifact_dir)
```

Add an AST/import test that `mimo_v2_replay.py` does not import modules whose
names contain `auto_trade`, `deepcoin`, `listener`, `telegram_sync`,
`notification`, `operator_bot`, or `authoritative_recognition`.

**Step 2: Run runner tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py -k 'runner or disposable or prohibited_import'
```

Expected: FAIL because `run_mimo_v2_replay` is missing.

**Step 3: Implement the minimal replay runner**

Add:

```python
@dataclass(frozen=True, slots=True)
class MimoV2ReplayResult:
    processed: int
    comparable: int
    unsafe_mismatches: int
    validation_failures: int
    production_writes: int
    notifications_sent: int
    execution_calls: int
    passed: bool
    comparisons: tuple[ReplayComparisonRow, ...]
    performance: ReplayPerformanceGate
    artifact_dir: Path


def run_mimo_v2_replay(
    *,
    inputs: MimoV2ReplayInputs,
    ai_recognition_config_path: str | Path,
    v1_runner=run_mimo_authoritative_for_message,
    v2_runner=infer_mimo_authoritative_v2,
    clock=time.perf_counter,
) -> MimoV2ReplayResult:
    ...
```

Implementation order:

1. Create `TemporaryDirectory(prefix=".working-", dir=artifact_dir)` and chmod
   it `0700`.
2. Online-backup source into `working.db`.
3. Call `create_session_factory(working.db)` so any additive schema/prompt/run
   writes affect only the copy.
4. Validate all selected `RawMessage` and referenced image paths before calls.
5. Build each message's authoritative context once with
   `build_authoritative_context_for_message`.
6. Time `run_mimo_authoritative_for_message(..., context_text=context)`.
7. Time `infer_mimo_authoritative_v2(..., context_text=context)` with bounded
   existing attempts.
8. On v2 success, independently time
   `adapt_mimo_v2_to_current_payload(v2.parsed_result)` even though inference
   already validated an adapter result; use this freshly adapted payload for
   comparison so adapter P95 is measured directly.
9. Store only IDs, stable statuses/error codes, durations and projection
   fingerprints in `ReplayComparisonRow`.
10. Dispose the working-copy engine before the temporary directory exits.

Do not persist replay rows in the database and do not call evidence finalizers,
authority save/claim/apply functions, candidates, lifecycle code, or settings
writers.

For provider/contract/adapter/image failures, record stable existing error
codes only. Never place exception text or provider payloads in the result.

**Step 4: Run runner tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 5: Add failure-classification tests**

Cover successful match, unsafe mismatch, v1 failure, v2 provider failure,
contract failure, adapter failure, missing image, and an unexpected runner
exception. Require each non-success to increase `validation_failures` or
`unsafe_mismatches`, make `passed=False`, and leave later bounded messages
processable unless the isolation boundary itself failed.

**Step 6: Run all replay tests and commit**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_mimo_v2_replay.py
git diff --check
```

Expected: PASS.

Commit:

```bash
git add src/telegram_kol_research/mimo_v2_replay.py \
  tests/test_mimo_v2_replay.py
git commit -m "feat: run isolated mimo comparison"
```

### Task 4: Write redacted artifacts and expose the strict CLI

**Files:**
- Modify: `src/telegram_kol_research/mimo_v2_replay.py`
- Modify: `src/telegram_kol_research/cli.py:1942`
- Modify: `tests/test_mimo_v2_replay.py`
- Modify: `tests/test_cli_smoke.py`

**Step 1: Write failing artifact tests**

Require `comparisons.json`, `comparisons.csv`, and `summary.json`; deterministic
field order; atomic replacement; and no other retained files. Seed source text,
base64-like image content, prompt text, bearer tokens, API keys, passwords, and
raw provider response markers, then assert none occur in any retained file.

```python
def test_artifacts_are_bounded_and_redacted(tmp_path):
    result = run_mimo_v2_replay(...)

    assert sorted(path.name for path in result.artifact_dir.iterdir()) == [
        "comparisons.csv", "comparisons.json", "summary.json"
    ]
    retained = "".join(
        path.read_text(encoding="utf-8")
        for path in result.artifact_dir.iterdir()
    )
    assert "data:image" not in retained
    assert "Bearer" not in retained
    assert seeded_source_text not in retained
```

**Step 2: Run artifact tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py -k artifact
```

Expected: FAIL because artifact writers are missing.

**Step 3: Implement atomic bounded artifact serialization**

Serialize explicit allowlisted fields only. JSON rows may contain:

```text
raw_message_id, status, reason_code, v1_status, v2_status,
v1_duration_ms, v2_duration_ms, adapter_duration_ms,
v1_projection_fingerprint, v2_projection_fingerprint
```

Summary may contain only counts, P95 values, ratio, gate codes, invariant zero
counters, schema version and overall `passed`. CSV uses the same row allowlist.
Write `.<name>.tmp`, flush/fsync, and `os.replace` within the artifact
directory. Reject non-finite values before serialization.

**Step 4: Run artifact tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

**Step 5: Write failing CLI tests**

Test required options/help, input error exit `2`, semantic/performance failure
exit `1`, passing replay exit `0`, compact JSON stdout, and no notifier or
Deepcoin constructor invocation.

```python
def test_replay_mimo_v2_cli_exits_nonzero_on_unsafe_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        replay_module,
        "run_mimo_v2_replay",
        lambda **kwargs: failed_replay_result(),
    )
    result = CliRunner().invoke(app, ["replay-mimo-v2", ...])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["passed"] is False
```

Patch `build_deepcoin_client_from_env`, notification loaders/senders, listener
entry points, and `process_authoritative_message` to raise if called.

**Step 6: Run CLI tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_cli_smoke.py -k mimo_v2_replay
```

Expected: FAIL because the command is missing.

**Step 7: Add the lazy, explicit CLI command**

Register `replay-mimo-v2` near the existing `mimo-experiment` command. Require
options with no production defaults:

```python
@app.command("replay-mimo-v2")
def replay_mimo_v2(
    database: Path = typer.Option(..., "--database"),
    message_id_file: Path = typer.Option(..., "--message-id-file"),
    media_root: Path = typer.Option(..., "--media-root"),
    artifact_dir: Path = typer.Option(..., "--artifact-dir"),
    ai_config_path: Path = typer.Option(..., "--ai-config-path"),
    max_messages: int = typer.Option(200, "--max-messages", min=1, max=200),
) -> None:
    from telegram_kol_research.mimo_v2_replay import (
        MimoV2ReplayInputError,
        run_mimo_v2_replay,
        validate_replay_inputs,
    )
    ...
```

Validate before loading AI configuration or making a model call. Print the
allowlisted summary JSON. Exit `2` for input/isolation errors and `1` when the
completed replay fails any semantic, validation, or performance gate.

Do not add a Web button, server action, settings mutation, notification, or
automatic artifact upload.

**Step 8: Run all Task 12 tests and focused safety regressions**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_mimo_v2_replay.py \
  tests/test_cli_smoke.py -k 'mimo_v2_replay or cli_help'
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_recognition_experiments.py \
  tests/test_mimo_v2_execution_adapter.py \
  tests/test_authoritative_recognition.py
git diff --check
```

Expected: PASS with no network calls, production writes, notifications, or
whitespace errors.

**Step 9: Review and commit Task 12**

Review specifically for:

- source path never passed to `create_session_factory`;
- temporary database cleanup on every exit;
- no execution/writer/listener/notifier import in the replay module;
- artifact allowlists rather than recursive object serialization;
- exact nonzero exit behavior; and
- production mode/settings untouched.

Commit:

```bash
git add src/telegram_kol_research/mimo_v2_replay.py \
  src/telegram_kol_research/cli.py \
  tests/test_mimo_v2_replay.py tests/test_cli_smoke.py
git commit -m "feat: add isolated mimo v2 replay"
```

### Task 5: Stop before server execution

**Files:**
- Review only: Task 12 commits and retained artifacts from tests

**Step 1: Confirm local branch state**

Run:

```bash
git status --short
git log --oneline --decorate -8
```

Expected: clean worktree and Task 12 commits only after the reviewed Task 11
checkpoint/design commits.

**Step 2: Confirm production remains untouched**

Read only:

```bash
ssh -i /Users/steven/.ssh/tecent.pem root@43.167.220.225 \
  'git -C /opt/telegram-kol-analyzer rev-parse HEAD; \
   systemctl is-active telegram-kol.service'
```

Query only `mimo_contract_mode` and confirm it remains `v1`. Do not deploy,
restart, push, build a production corpus, or run replay on the server.

**Step 3: Report the Task 12 implementation checkpoint**

Report commits, focused tests, review findings, production read-only status,
and the fact that server replay still requires the later reviewed deployment
and safe-window workflow.
