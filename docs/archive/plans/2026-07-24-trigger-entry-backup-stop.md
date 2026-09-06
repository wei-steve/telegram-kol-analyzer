# Trigger Entry Backup Stop Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give each newly filled Deepcoin trigger-entry split position a second, exact-position conditional market-close stop 50 bps beyond its attached primary stop, and durably surface any stop that triggers unsuccessfully.

**Architecture:** The trigger parent retains its attached Deepcoin stop for immediate protection. Once reconciliation verifies the exact \`posId\`, a new \`PositionBackupStopOrder\` record owns one separate \`/deepcoin/trade/trigger-order\` close condition using \`closePosId\`. Reconciliation evaluates both primary TPSL and backup order states against the exact live position and records a fail-closed incident rather than treating historical orders as active protection.

**Tech Stack:** Python 3.12, SQLAlchemy/SQLite, Deepcoin REST client, pytest.

---

### Task 1: Add durable backup-stop and protection-incident records

**Files:**

- Modify: \`src/telegram_kol_research/models.py:701-963\`
- Modify: \`src/telegram_kol_research/db.py:35-360\`
- Test: \`tests/test_protection_ledger.py\`

**Step 1: Write the failing tests**

Assert bootstrap creates \`position_backup_stop_orders\` and \`position_protection_incidents\`. The backup table must enforce one active backup per venue/position and unique exchange order IDs; incidents must have a unique fingerprint.

**Step 2: Run the test to verify it fails**

Run: \`uv run pytest -q tests/test_protection_ledger.py\`

Expected: FAIL because the tables do not exist.

**Step 3: Implement the minimal models**

Add \`PositionBackupStopOrder\` with exact binding, entry-leg, \`pos_id\`, trigger price, order/client IDs, status, request/response/error evidence, and timestamps. Add \`PositionProtectionIncident\` with exact owner IDs, incident type, fingerprint, redacted exchange evidence, delivery state, and timestamps. Register both with model metadata and SQLite bootstrap.

**Step 4: Run the test to verify it passes**

Run: \`uv run pytest -q tests/test_protection_ledger.py\`

Expected: PASS.

**Step 5: Commit**

\`\`\`bash
git add src/telegram_kol_research/models.py src/telegram_kol_research/db.py tests/test_protection_ledger.py
git commit -m "feat: persist trigger backup stop ownership"
\`\`\`

### Task 2: Calculate and construct an exact backup close trigger

**Files:**

- Create: \`src/telegram_kol_research/trigger_backup_stop.py\`
- Modify: \`src/telegram_kol_research/trading_settings.py\`
- Test: \`tests/test_trigger_backup_stop.py\`

**Step 1: Write the failing tests**

With a 50-bps buffer, assert long stop \`1919\` becomes \`1909.4\` at a \`0.1\` tick and short stop \`1919\` becomes \`1928.6\`. Assert long values round downward and short values upward. Refuse invalid side/price, a backup on the wrong risk side, and a backup beyond the safe side of liquidation.

**Step 2: Run the test to verify it fails**

Run: \`uv run pytest -q tests/test_trigger_backup_stop.py\`

Expected: FAIL because the module does not exist.

**Step 3: Implement the minimal helpers**

Add \`trigger_backup_stop_buffer_bps\` to trading settings with default \`50\`. Build a pure calculator and a payload builder for \`trigger_order\`: opposite close side, \`posSide\`, \`mrgPosition=split\`, \`tdMode\`, exact \`closePosId\`, market execution, and a unique client ID. Do not call \`set-position-sltp\`.

**Step 4: Run the test to verify it passes**

Run: \`uv run pytest -q tests/test_trigger_backup_stop.py\`

Expected: PASS.

**Step 5: Commit**

\`\`\`bash
git add src/telegram_kol_research/trigger_backup_stop.py src/telegram_kol_research/trading_settings.py tests/test_trigger_backup_stop.py
git commit -m "feat: build exact trigger backup stops"
\`\`\`

### Task 3: Submit one backup only after exact-position verification

**Files:**

- Modify: \`src/telegram_kol_research/execution_bindings.py:540-1035\`
- Modify: \`src/telegram_kol_research/deepcoin_client.py:67-130,250-410\`
- Modify: \`src/telegram_kol_research/execution_events.py\`
- Test: \`tests/test_execution_bindings.py\`
- Test: \`tests/test_deepcoin_client.py\`

**Step 1: Write the failing tests**

Use a fake client with a verified trigger entry, exact live split position, contract tick, and active attached stop. Assert reconciliation submits exactly one close trigger with the exact \`closePosId\`, persists the result, and records \`create_backup_stop\`. Reconcile twice and assert one submission only. Add no-write refusals for missing primary stop, ambiguous owner, missing exact position, crossed primary stop, and unsafe liquidation boundary.

**Step 2: Run the test to verify it fails**

Run: \`uv run pytest -q tests/test_execution_bindings.py tests/test_deepcoin_client.py\`

Expected: FAIL because reconciliation has no backup submission phase.

**Step 3: Implement the minimal submission phase**

After exact attribution and primary-stop proof, hold an account/instrument/side lock; re-read the exact position; create a durable \`submitting\` backup row; submit its trigger order; persist the returned ID and execution event. Preserve \`unknown_exchange_outcome\` without blind retries. Gate submission to newly opened trigger entries only for the initial rollout.

**Step 4: Run the test to verify it passes**

Run: \`uv run pytest -q tests/test_execution_bindings.py tests/test_deepcoin_client.py\`

Expected: PASS.

**Step 5: Commit**

\`\`\`bash
git add src/telegram_kol_research/execution_bindings.py src/telegram_kol_research/deepcoin_client.py src/telegram_kol_research/execution_events.py tests/test_execution_bindings.py tests/test_deepcoin_client.py
git commit -m "feat: submit exact trigger backup stops"
\`\`\`

### Task 4: Detect failed stop execution and freeze the exact position

**Files:**

- Modify: \`src/telegram_kol_research/execution_bindings.py:540-1035\`
- Modify: \`src/telegram_kol_research/strategy_management_planner.py\`
- Modify: \`src/telegram_kol_research/strategy_management_executor.py\`
- Modify: \`src/telegram_kol_research/system_operator_bot.py\`
- Test: \`tests/test_execution_bindings.py\`
- Test: \`tests/test_strategy_management_worker.py\`

**Step 1: Write the failing tests**

Seed a live exact position and primary stop history with \`triggerTime > 0\`, \`errorCode="203"\`, and \`errorMsg="NotEnoughMoneyToClose"\`. Assert the primary protection becomes failed, one \`stop_trigger_failed\` incident is created, and management refuses with \`protection_recovery_required\`. Repeating the snapshot must not duplicate incident or notification. Add pending-backup-after-primary-failure, missing order, query-error, and successful close cases.

**Step 2: Run the tests to verify they fail**

Run: \`uv run pytest -q tests/test_execution_bindings.py tests/test_strategy_management_worker.py\`

Expected: FAIL because history errors remain verified protection.

**Step 3: Implement lifecycle classification**

Compare pending and history snapshots against every exact owned position. A nonzero history error with a still-live position produces \`stop_trigger_failed\`; a missing order produces \`protection_missing\`; a fetch error produces \`protection_unknown\`. Persist a fingerprinted incident, freeze automatic management, and send one high-priority alert. This detector must not submit replacement stops or close orders.

**Step 4: Run the tests to verify they pass**

Run: \`uv run pytest -q tests/test_execution_bindings.py tests/test_strategy_management_worker.py\`

Expected: PASS.

**Step 5: Commit**

\`\`\`bash
git add src/telegram_kol_research/execution_bindings.py src/telegram_kol_research/strategy_management_planner.py src/telegram_kol_research/strategy_management_executor.py src/telegram_kol_research/system_operator_bot.py tests/test_execution_bindings.py tests/test_strategy_management_worker.py
git commit -m "fix: fail closed when trigger stop rejects"
\`\`\`

### Task 5: Project evidence and clean up only exact owned siblings

**Files:**

- Modify: \`src/telegram_kol_research/strategy_records.py\`
- Modify: \`src/telegram_kol_research/web_queries.py\`
- Modify: \`src/telegram_kol_research/templates/_strategy_detail.html\`
- Test: \`tests/test_strategy_records.py\`
- Test: \`tests/test_web_strategy_records.py\`

**Step 1: Write failing tests**

Create a strategy detail with primary and backup order IDs and a failed-stop incident. Assert both roles, prices, exact position, current state, and redacted error summary are displayed.

**Step 2: Run the tests to verify they fail**

Run: \`uv run pytest -q tests/test_strategy_records.py tests/test_web_strategy_records.py\`

Expected: FAIL because the backup and incident have no projection.

**Step 3: Implement read-only projection and terminal cleanup**

Show both protections and incident state. After a position is terminally proven, cancel only its persisted system-owned sibling order by exact ID. Retain unknown orders and outcomes for investigation.

**Step 4: Run the tests to verify they pass**

Run: \`uv run pytest -q tests/test_strategy_records.py tests/test_web_strategy_records.py\`

Expected: PASS.

**Step 5: Commit**

\`\`\`bash
git add src/telegram_kol_research/strategy_records.py src/telegram_kol_research/web_queries.py src/telegram_kol_research/templates/_strategy_detail.html tests/test_strategy_records.py tests/test_web_strategy_records.py
git commit -m "feat: show trigger backup stop evidence"
\`\`\`

### Task 6: Validate and roll out safely

**Files:**

- Modify: \`docs/plans/2026-07-24-trigger-entry-backup-stop-design.md\`
- Modify: \`docs/plans/2026-07-24-trigger-entry-backup-stop.md\`

**Step 1: Run focused coverage**

Run:

\`\`\`bash
uv run pytest -q tests/test_protection_ledger.py tests/test_trigger_backup_stop.py tests/test_execution_bindings.py tests/test_strategy_management_worker.py tests/test_strategy_records.py tests/test_web_strategy_records.py tests/test_deepcoin_client.py
\`\`\`

Expected: PASS.

**Step 2: Run the full local suite**

Run: \`uv run pytest -q\`

Expected: PASS.

**Step 3: Review for unintended writes**

Run: \`git diff --check && git diff -- src/telegram_kol_research tests docs/plans/2026-07-24-trigger-entry-backup-stop*\`

Expected: only exact-position backup protection, failure detection, tests, and documentation.

**Step 4: Deploy in stages**

Push reviewed commits to \`codex/deepcoin-auto-trading-v1\`, deploy with \`scripts/server_git_update.ps1\`, and initially enable only read-only incident detection. Verify a fresh server snapshot. Enable backup submission only for new trigger entries after confirming both orders and exact ownership. Never retrofit current holdings without separate approval.

**Step 5: Commit documentation**

\`\`\`bash
git add docs/plans/2026-07-24-trigger-entry-backup-stop-design.md docs/plans/2026-07-24-trigger-entry-backup-stop.md
git commit -m "docs: plan trigger backup stop rollout"
\`\`\`

