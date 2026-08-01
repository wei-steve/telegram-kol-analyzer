# KOL Strategy PnL Audit Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce a deterministic, read-only BTC/ETH strategy ledger and PnL audit for `币圈所长会员群-11分组` from raw Telegram evidence and public candles.

**Architecture:** Add a pure audit domain module for normalized strategies, event attribution, candle replay, and summaries. Keep production data access outside the domain: the CLI consumes a JSON message snapshot from a file or stdin, fetches/caches public Binance candles locally, and writes ignored JSON/Markdown artifacts without modifying SQLite. A reviewed decisions file handles genuinely ambiguous KOL-specific inclusions, splits, merges, corrections, and event links while keeping every judgment explicit and replayable.

**Tech Stack:** Python 3.12, dataclasses, Decimal, SQLAlchemy read-only snapshot query, Typer, httpx, JSON, pytest

---

### Task 1: Define the audit contracts and validation boundary

**Files:**
- Create: `src/telegram_kol_research/kol_pnl_audit.py`
- Create: `tests/test_kol_pnl_audit.py`

**Step 1: Write the failing contract tests**

Add tests that construct a `NormalizedAuditStrategy` with stable identity,
message evidence, entry legs, stop rule, targets, and management events. Assert
that:

```python
strategy = NormalizedAuditStrategy.from_dict(payload)
assert strategy.audit_id == "-1002368892075:6496:BTC:long:1"
assert strategy.take_profit_allocations == (
    Decimal("40"), Decimal("20"), Decimal("20"), Decimal("20")
)
```

Add rejection tests for missing evidence IDs, invalid side, non-positive prices,
stop on the profitable side of entry, target ordering opposite the strategy,
allocations not totaling 100, and more than five targets.

**Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_audit.py -k "contract or validation"
```

Expected: FAIL because `telegram_kol_research.kol_pnl_audit` does not exist.

**Step 3: Implement the minimal immutable contracts**

Add frozen dataclasses and JSON conversion for:

```python
AuditMessageEvidence
AuditEntryLeg
AuditStopRule
AuditTakeProfit
AuditManagementEvent
NormalizedAuditStrategy
AuditValidationError
```

Use `Decimal` for prices, allocations, returns, and `R`. Reuse
`build_take_profit_plan()` for the approved default allocations. Keep every
timestamp UTC-aware at the boundary.

**Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_audit.py -k "contract or validation"
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/kol_pnl_audit.py tests/test_kol_pnl_audit.py
git commit -m "feat: add KOL PnL audit contracts"
```

### Task 2: Reconstruct logical strategies from messages and reviewed decisions

**Files:**
- Modify: `src/telegram_kol_research/kol_pnl_audit.py`
- Modify: `tests/test_kol_pnl_audit.py`
- Create: `tests/fixtures/kol_pnl_audit/messages.json`
- Create: `tests/fixtures/kol_pnl_audit/decisions.json`

**Step 1: Write failing reconstruction tests**

Use redacted fixtures representing the observed cases:

- one ordinary BTC strategy;
- a promotional profit post that must be excluded;
- a duplicate repost that extends validity but does not add a trade;
- one message with BTC short and BTC long instructions that must split;
- one message with ETH short and ETH long instructions that must split;
- a target update, explicit protection, cancellation, partial exit, and full exit;
- an ambiguous lifecycle event that must remain unresolved;
- an obvious numeric typo that is corrected only by a reviewed decision.

Assert stable audit IDs and exact evidence/reason codes such as
`duplicate_continuation`, `composite_strategy_split`, `promotional_excluded`,
`reviewed_numeric_correction`, and `ambiguous_event_target`.

**Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_audit.py -k reconstruction
```

Expected: FAIL because reconstruction helpers are missing.

**Step 3: Implement deterministic reconstruction**

Add:

```python
load_audit_messages(payload) -> tuple[AuditSourceMessage, ...]
load_reviewed_decisions(payload) -> AuditDecisionSet
reconstruct_audit_strategies(messages, decisions) -> AuditReconstruction
```

The automatic pass may identify explicit BTC/ETH entry messages and candidate
management messages, but it must fail closed. The decisions file is the only
place allowed to split, merge, exclude, correct, or manually link ambiguous
records. Do not call an LLM inside the audit.

**Step 4: Run focused and parser regression tests**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_audit.py -k reconstruction
uv run pytest -q tests/test_message_recognition.py -k "junzhang or composite or multi_target"
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/kol_pnl_audit.py tests/test_kol_pnl_audit.py tests/fixtures/kol_pnl_audit
git commit -m "feat: reconstruct reviewed KOL audit strategies"
```

### Task 3: Add immutable public candle loading and cache verification

**Files:**
- Create: `src/telegram_kol_research/kol_audit_market_data.py`
- Create: `tests/test_kol_audit_market_data.py`

**Step 1: Write failing market-data tests**

Test pagination, UTC normalization, chronological de-duplication, symbol
allowlisting, interval allowlisting, SHA-256 cache metadata, exact cache replay,
and fail-closed behavior for a missing candle interval. Use `httpx.MockTransport`;
tests must not access the network.

Expected public API request shape:

```python
GET /api/v3/klines
  ?symbol=BTCUSDT
  &interval=5m
  &startTime=<epoch-ms>
  &endTime=<epoch-ms>
  &limit=1000
```

**Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_kol_audit_market_data.py
```

Expected: FAIL because the adapter does not exist.

**Step 3: Implement the read-only adapter**

Add `BinanceAuditMarketData`, `AuditCandle`, and `CandleEvidenceManifest`.
Fetch the smallest required base interval, retain OHLC and close timestamps,
paginate without gaps, and write cache files atomically. Loading cached evidence
must verify its digest before returning candles.

**Step 4: Run the tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_kol_audit_market_data.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/kol_audit_market_data.py tests/test_kol_audit_market_data.py
git commit -m "feat: add immutable KOL audit candle evidence"
```

### Task 4: Implement entry, exit, and allocation-aware replay

**Files:**
- Modify: `src/telegram_kol_research/kol_pnl_audit.py`
- Modify: `tests/test_kol_pnl_audit.py`

**Step 1: Write failing replay tests**

Add one focused test per behavior:

- no fill before message publication;
- single entry and two-leg weighted entry;
- unfilled second leg remains pending;
- two through five targets use approved allocations;
- explicit target allocation overrides defaults;
- touch stop and 5m/15m/1h close-qualified stops;
- partial target without protection keeps the original stop;
- explicit protection moves only remaining allocation to break-even;
- explicit partial and full exits use the first candle after the message;
- replacement/cancellation stops future fills;
- same-candle stop/target ambiguity chooses the adverse result;
- open and unresolved strategies do not enter strict summary PnL.

Use exact `Decimal` expectations, for example:

```python
assert result.realized_r == Decimal("1.25")
assert result.reason_codes == ("target_1", "explicit_break_even", "target_2")
```

**Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_audit.py -k replay
```

Expected: FAIL because replay is not implemented.

**Step 3: Implement the pure replay engine**

Add:

```python
replay_audit_strategy(strategy, candles, cutoff) -> AuditStrategyResult
```

Process timestamped message events and candles in a single ordered stream.
Preserve the initial risk denominator after fills, allocate every exit exactly
once, and require total closed plus open allocation to equal 100. Round only in
rendering, never during replay.

**Step 4: Run replay and full audit tests**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_audit.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/kol_pnl_audit.py tests/test_kol_pnl_audit.py
git commit -m "feat: replay KOL strategies for audited PnL"
```

### Task 5: Add strict summaries, lifecycle comparison, and reports

**Files:**
- Create: `src/telegram_kol_research/kol_pnl_reporting.py`
- Create: `tests/test_kol_pnl_reporting.py`
- Create: `tests/fixtures/kol_pnl_audit/lifecycles.json`

**Step 1: Write failing reporting tests**

Assert BTC, ETH, and combined summaries for count, entered, unresolved,
profitable/loss/break-even, win rate, cumulative and average `R`, profit factor,
maximum `R` drawdown, and maximum loss streak. Confirm only high/medium
confidence closed rows enter strict headline metrics.

Add lifecycle comparison tests for missing strategy, duplicate lifecycle,
wrong price, impossible timestamp ordering, wrong status, and missing management
event. Assert JSON is deterministic and Markdown includes per-strategy evidence
and discrepancy tables.

**Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_reporting.py
```

Expected: FAIL because reporting helpers are missing.

**Step 3: Implement summary and rendering helpers**

Add:

```python
summarize_audit_results(results, confidence=("high", "medium"))
compare_lifecycle_snapshot(reconstruction, lifecycle_rows)
render_audit_json(...)
render_audit_markdown(...)
write_audit_artifacts(...)
```

Use atomic writes and include audit cutoff, source snapshot digest, candle digest,
decision digest, code revision, and methodology version in every final artifact.

**Step 4: Run tests and verify GREEN**

Run:

```bash
uv run pytest -q tests/test_kol_pnl_reporting.py tests/test_kol_pnl_audit.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/kol_pnl_reporting.py tests/test_kol_pnl_reporting.py tests/fixtures/kol_pnl_audit/lifecycles.json
git commit -m "feat: report audited KOL performance"
```

### Task 6: Add a read-only CLI workflow

**Files:**
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `.gitignore`
- Create: `tests/test_cli_kol_pnl_audit.py`
- Modify: `README.md`

**Step 1: Write failing CLI tests**

Test that `audit-kol-pnl`:

- accepts `--messages-json -` from stdin;
- accepts optional lifecycle JSON and required reviewed decisions;
- requires BTC/ETH allowlisting, chat ID, cutoff, and an output directory;
- defaults to dry, local artifact creation only;
- refuses an output directory outside the requested bounded target;
- never opens the project SQLite database for write;
- supports `--offline` deterministic cache replay;
- reports unresolved evidence without claiming final success.

**Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest -q tests/test_cli_kol_pnl_audit.py
```

Expected: FAIL because the command is absent.

**Step 3: Implement the CLI and ignored output boundary**

Add `.audit-results/` to `.gitignore`. Wire the pure modules into:

```bash
telegram-kol-research audit-kol-pnl \
  --messages-json - \
  --decisions-json .audit-results/suozhang/decisions.json \
  --lifecycle-json .audit-results/suozhang/lifecycles.json \
  --chat-id=-1002368892075 \
  --symbol BTC --symbol ETH \
  --cutoff 2026-08-01T02:19:12Z \
  --output-dir .audit-results/suozhang
```

The command has no production database mutation option.

**Step 4: Run focused and CLI regression tests**

Run:

```bash
uv run pytest -q tests/test_cli_kol_pnl_audit.py tests/test_cli_smoke.py tests/test_report_cli_db.py
```

Expected: PASS.

**Step 5: Update operator documentation**

Document the read-only server snapshot query, stdin workflow, decisions review,
online candle capture, offline replay, output interpretation, and explicit
statement that this command does not repair lifecycle data or calculate actual
Deepcoin account PnL.

**Step 6: Commit**

```bash
git add .gitignore README.md src/telegram_kol_research/cli.py tests/test_cli_kol_pnl_audit.py
git commit -m "feat: add read-only KOL PnL audit command"
```

### Task 7: Build and review the 所长 evidence ledger

**Files:**
- Create locally, ignored: `.audit-results/suozhang/messages.json`
- Create locally, ignored: `.audit-results/suozhang/lifecycles.json`
- Create locally, ignored: `.audit-results/suozhang/decisions.json`
- Create locally, ignored: `.audit-results/suozhang/reconstruction.json`

**Step 1: Export bounded read-only snapshots**

Resolve the exact server database and chat ID first. Run SQLite with
`PRAGMA query_only=ON`, selecting only target-chat `raw_messages` and comparison
fields from `strategy_lifecycles`. Do not copy the database file.

**Step 2: Generate a draft reconstruction**

Run the audit in reconstruction-only mode. Review every included strategy,
excluded candidate, split composite, duplicate, correction, and linked event
against its original message context.

**Step 3: Complete the decisions ledger**

Record all manual judgments with source message IDs and concise reason codes.
Specifically review the previously observed gaps and composite messages,
including `6496`, `6513`, `6517`, `6535`, `6537`, `6549`, `6553`, `6728`,
`6731/6732`, `6738`, `6765`, `6777`, `6857`, `6897`, `6908`, `6930`, and
`6983`.

**Step 4: Verify reconstruction determinism**

Run reconstruction twice and compare SHA-256 digests. Expected: identical
normalized ledger and no unreviewed high-confidence candidate.

### Task 8: Run the final read-only audit and verify the result

**Files:**
- Create locally, ignored: `.audit-results/suozhang/candles/*.json`
- Create locally, ignored: `.audit-results/suozhang/results.json`
- Create locally, ignored: `.audit-results/suozhang/report.md`

**Step 1: Capture candle evidence and calculate results**

Run the approved audit cutoff first, then rerun with `--offline` against the
captured candle evidence.

Expected: online and offline result digests match.

**Step 2: Perform manual spot checks**

Check at least:

- two BTC wins, two BTC losses, and one unfilled BTC strategy;
- two ETH wins, two ETH losses, and one unresolved ETH strategy;
- one range entry, one composite message, one explicit protection, one
  close-qualified stop, and one manual exit.

For each, independently inspect the relevant public candles and Telegram source
messages.

**Step 3: Run the complete local test suite**

Run:

```bash
uv run pytest -q
```

Expected: PASS.

**Step 4: Request code review**

Use the `requesting-code-review` skill against the implementation base and head
SHAs. Fix every Critical and Important finding, then rerun focused tests and the
full suite.

**Step 5: Commit final code/documentation corrections**

Do not commit private audit artifacts. Confirm with:

```bash
git status --short
git check-ignore .audit-results/suozhang/results.json
```

Commit only reviewed source, tests, and documentation changes.

### Task 9: Publish reviewed code without unsafe production activity

**Files:**
- No private audit artifacts

**Step 1: Confirm branch and commit scope**

Verify the branch is `codex/deepcoin-auto-trading-v1`, inspect every commit, and
ensure unrelated user changes remain untouched.

**Step 2: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds.

**Step 3: Do not deploy merely to validate the read-only report**

The audit runs locally from bounded read-only snapshots, so no production
restart is required. If a later request asks to install the command on the
server, first prove there is no active time-sensitive strategy operation, then
use the repository deployment helper and perform server verification.

**Step 4: Hand off the result**

Report BTC/ETH strict metrics, confidence exclusions, the artifact locations,
the snapshot/candle/result digests, material recognition discrepancies, tests,
commits, and push status. Clearly distinguish theoretical strategy PnL from
actual Deepcoin account PnL.
