# Position-to-Message Compliance Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make every new risk-reducing Telegram management message produce exact target-specific work or a durable operator-visible failure, and make protection replacement converge all authoritative role ledgers before success.

**Architecture:** Repair the existing authoritative MiMo/context/management path rather than adding a second strategy resolver. Split operator alerts from ordinary notification delivery, persist bounded terminal context failures for the Runtime Agent, and reuse the role-aware position-mutation gateway for primary/backup protection replacement. Historical incident handling is a separate read-only convergence stage and never replays an old message.

**Tech Stack:** Python 3.12, SQLAlchemy, SQLite, FastAPI, Telethon, pytest, systemd, Deepcoin REST client.

---

## Execution constraints

- Use test-driven development for every task.
- Do not replay Telegram message 3465 or submit a live test order.
- Keep Runtime Agent action authority false and both playbook allowlists empty.
- Deploy and enable at most one runtime stage in one user turn.
- Before every production restart, prove there is no time-sensitive strategy
  recognition, management, protection, exit, reconciliation, or recovery work
  in flight.
- If a safe deployment window cannot be proven, stop after local tests, review,
  commit, and push; record the remaining server checks without restarting.
- Commit only files belonging to the current task. Preserve the existing dirty
  worktree and unrelated user artifacts.

### Task 1: Split operator-alert and ordinary-notification routing

**Files:**
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_web_live_listener_startup.py`
- Modify: `tests/test_telegram_live_listener.py`
- Modify: `tests/test_web_app.py`

**Step 1: Write the failing startup-routing test**

Update `tests/test_web_live_listener_startup.py` so the fake listener accepts
both named configurations and asserts that they are not substituted for one
another:

```python
async def fake_live_listener_runner(
    *, system_operator_bot_config=None, notification_bot_config=None, **kwargs
):
    calls.append((system_operator_bot_config, notification_bot_config))

assert calls == [(app.state.system_operator_bot_config,
                  app.state.notification_bot_config)]
```

Add a second case with a configured system-operator bot and
`notification_bot_config=None`; the listener must still receive the operator
configuration.

**Step 2: Run the focused test and verify failure**

Run:

```bash
uv run pytest tests/test_web_live_listener_startup.py -q
```

Expected: FAIL because the live-listener runner currently receives only the
ordinary notification configuration.

**Step 3: Separate the listener parameters**

Change `run_live_listener` and its launch wrapper to accept:

```python
system_operator_bot_config: SystemOperatorBotConfig | None = None,
notification_bot_config: SystemOperatorBotConfig | None = None,
```

Use `system_operator_bot_config` only for authoritative failures and protection,
attribution, and incident alerts. Use `notification_bot_config` only for normal
instruction summaries.

Pass both explicit values from `create_web_app` startup, reconcile startup,
manual recognition, and any recovery listener call sites. Do not implement an
implicit chat-ID fallback.

**Step 4: Add listener behavior tests**

In `tests/test_telegram_live_listener.py`, add:

```python
def test_high_risk_authoritative_failure_uses_operator_bot_when_summary_bot_absent(...):
    # Recognition fails for "BTC空单止盈一部分".
    # system_operator_bot_config is present; notification_bot_config is None.
    # Assert exactly one operator failure alert and no summary delivery.
```

In `tests/test_web_app.py`, add the same assertion for the manual-recognition
endpoint.

**Step 5: Run notification and web tests**

Run:

```bash
uv run pytest \
  tests/test_web_live_listener_startup.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_app.py -q
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/telegram_live_listener.py \
  src/telegram_kol_research/web_app.py \
  tests/test_web_live_listener_startup.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_app.py
git commit -m "fix: route trading failures to operator bot"
```

### Task 2: Persist closed context-error codes and retry contract failures

**Files:**
- Modify: `src/telegram_kol_research/context_resolution.py`
- Modify: `src/telegram_kol_research/runtime_incident_adapters.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `tests/test_context_resolution.py`
- Modify: `tests/test_runtime_incident_adapters.py`
- Modify: `tests/test_authoritative_recognition.py`

**Step 1: Write failing contract-retry tests**

Add tests to `tests/test_context_resolution.py` for a caller that returns one
invalid contract response and then a valid response. Assert two calls, a
completed attempt, and no incident.

Add a second test that returns the same closed contract error twice. Assert:

```python
assert attempt.status == "exhausted"
assert attempt.attempts == 2
assert attempt.error_class == "multi_target_action_not_allowed"
```

The provider response body must not be stored.

**Step 2: Run the tests and verify failure**

```bash
uv run pytest \
  tests/test_context_resolution.py \
  tests/test_runtime_incident_adapters.py -q
```

Expected: FAIL because `contract_error` exits after the first attempt and the
adapter captures only a worker-owned exhausted row.

**Step 3: Add a closed error taxonomy**

Give `ContextResolutionError` a bounded `code` field. Replace free-form parser
branches with closed codes such as:

```python
class ContextResolutionError(ValueError):
    def __init__(self, code: str):
        if code not in CONTEXT_RESOLUTION_ERROR_CODES:
            code = "context_contract_invalid"
        self.code = code
        super().__init__(code)
```

Keep error values free of provider bodies, message text, order payloads, and
credentials.

**Step 4: Retry all technical/contract classes once**

Refactor the two-attempt loop so network, JSON, and closed contract errors all
persist an intermediate retry state on attempt one and become `exhausted` on
attempt two. The retry must use the same context fingerprint, evidence version,
candidate IDs, and exchange projection.

On exhaustion, call `capture_runtime_incident_best_effort` with the existing
`capture_context_worker_state` adapter after the terminal attempt commit.

**Step 5: Keep authoritative failure notification independent**

In `tests/test_authoritative_recognition.py`, prove that exhaustion returns
`authoritative_failed` and a durable context incident even if the notification
sender later fails. The original context incident and the notification failure
must use separate fingerprints.

**Step 6: Run focused tests**

```bash
uv run pytest \
  tests/test_context_resolution.py \
  tests/test_context_resolution_worker.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_authoritative_recognition.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/context_resolution.py \
  src/telegram_kol_research/runtime_incident_adapters.py \
  src/telegram_kol_research/authoritative_recognition.py \
  tests/test_context_resolution.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_authoritative_recognition.py
git commit -m "fix: exhaust and capture context contract failures"
```

### Task 3: Allow exact multi-target partial take profit in contextual resolution

**Files:**
- Modify: `src/telegram_kol_research/context_resolution.py`
- Modify: `src/telegram_kol_research/context_resolution_prompt.py`
- Modify: `src/telegram_kol_research/authoritative_recognition.py`
- Modify: `src/telegram_kol_research/management_directives.py`
- Modify: `tests/test_context_resolution.py`
- Modify: `tests/test_context_resolution_prompt.py`
- Modify: `tests/test_authoritative_recognition.py`
- Modify: `tests/test_management_directives.py`

**Step 1: Write the failing two-target resolver test**

Create a valid `manage_thread` result with two exact thread IDs,
`management_action="partial_take_profit"`, and
`risk_reducing_fanout_allowed=true`. Assert the parser accepts it and preserves
both IDs in input order.

**Step 2: Write refusal tests**

Add parameterized cases proving that multi-target remains refused for:

- `move_stop_to_protect`;
- `risk_update`;
- `replace_entry`;
- mixed partial close plus add/reverse wording;
- a target outside `allowed_thread_ids`;
- duplicate targets.

**Step 3: Run the tests and verify failure**

```bash
uv run pytest \
  tests/test_context_resolution.py \
  tests/test_context_resolution_prompt.py \
  tests/test_management_directives.py -q
```

Expected: FAIL because only cancel/exit decisions currently allow fanout.

**Step 4: Encode the narrow policy**

Replace the broad decision-only set with a predicate equivalent to:

```python
def _fanout_allowed(decision: str, action: str | None) -> bool:
    return (
        decision in {"cancel_thread", "exit_thread"}
        or (decision == "manage_thread" and action == "partial_take_profit")
    )
```

Require the model boolean to be true and every target to remain inside the
closed candidate set. Update the prompt to state that multi-target
`partial_take_profit` is allowed only when the current message explicitly names
each independent target and all targets are risk reducing.

**Step 5: Preserve structured targets in the authoritative payload**

Make `_resolved_mimo_result` emit:

```python
"target_lifecycle_id": lifecycle_ids[0],
"targets": [
    {"target_lifecycle_id": lifecycle_id,
     "symbol": candidate.symbol,
     "side": candidate.side}
    for candidate in selected_candidates
],
```

Emit `targets` only when more than one lifecycle is selected. Do not infer a
symbol or side from the message alone.

**Step 6: Run focused tests**

```bash
uv run pytest \
  tests/test_context_resolution.py \
  tests/test_context_resolution_prompt.py \
  tests/test_authoritative_recognition.py \
  tests/test_management_directives.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/context_resolution.py \
  src/telegram_kol_research/context_resolution_prompt.py \
  src/telegram_kol_research/authoritative_recognition.py \
  src/telegram_kol_research/management_directives.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_prompt.py \
  tests/test_authoritative_recognition.py \
  tests/test_management_directives.py
git commit -m "fix: resolve exact multi-target profit taking"
```

### Task 4: Prove all-or-nothing target persistence and message idempotency

**Files:**
- Create: `tests/fixtures/context_resolution/shuqin_3465_multitarget.json`
- Modify: `src/telegram_kol_research/message_recognition.py`
- Modify: `src/telegram_kol_research/management_scope.py`
- Modify: `src/telegram_kol_research/message_instruction_items.py`
- Modify: `tests/test_message_recognition.py`
- Modify: `tests/test_management_scope.py`
- Modify: `tests/test_authoritative_recognition.py`

**Step 1: Add a bounded production-shaped fixture**

Create a redacted fixture containing only:

- message text `BTC ETH空单可以止盈一部分`;
- two same-chat active short lifecycles;
- exact verified entry-leg presence/counts;
- a position-before-message boolean;
- no real order IDs, position IDs, chat IDs, prompts, or provider output.

**Step 2: Write the failing atomicity test**

Start with two explicit targets but make the second target lack verified exact
ownership. Assert zero new candidates, instruction items, and management
batches. No first-target partial success is allowed.

**Step 3: Write the success and replay tests**

Use both valid targets and assert one candidate and instruction item per
lifecycle. Apply the same authoritative generation twice and assert counts do
not change.

**Step 4: Run and verify the failure**

```bash
uv run pytest \
  tests/test_message_recognition.py \
  tests/test_management_scope.py \
  tests/test_authoritative_recognition.py -q
```

Expected: at least the all-or-nothing test fails before implementation.

**Step 5: Add a single validation transaction**

Validate the complete explicit target set before inserting any target-specific
row. Reuse `resolve_management_scope_in_session` for exact ownership and
position-time checks. Create all `SignalCandidate` and `MessageInstructionItem`
rows in one transaction only after the full set passes.

Keep execution target-specific after persistence: one failed batch does not
rewrite another target's result, and the summary reports them separately.

**Step 6: Run focused tests**

```bash
uv run pytest \
  tests/test_message_recognition.py \
  tests/test_management_scope.py \
  tests/test_authoritative_recognition.py \
  tests/test_system_operator_bot.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add tests/fixtures/context_resolution/shuqin_3465_multitarget.json \
  src/telegram_kol_research/message_recognition.py \
  src/telegram_kol_research/management_scope.py \
  src/telegram_kol_research/message_instruction_items.py \
  tests/test_message_recognition.py \
  tests/test_management_scope.py \
  tests/test_authoritative_recognition.py \
  tests/test_system_operator_bot.py
git commit -m "fix: persist multi-target management atomically"
```

### Task 5: Converge primary and backup roles during protection replacement

**Files:**
- Create: `src/telegram_kol_research/protection_replacement_persistence.py`
- Modify: `src/telegram_kol_research/strategy_management_executor.py`
- Modify: `src/telegram_kol_research/strategy_management_composite_executor.py`
- Modify: `src/telegram_kol_research/position_mutation_gateway.py`
- Modify: `tests/test_strategy_management_executor.py`
- Modify: `tests/test_position_mutation_gateway.py`
- Modify: `tests/test_protection_ledger.py`
- Modify: `tests/test_position_protection_legs.py`
- Modify: `tests/test_protection_revisions.py`

**Step 1: Write the production-shaped failing replacement test**

In `tests/test_strategy_management_executor.py`, reproduce one exact long
position with:

- old primary stop;
- old backup stop;
- one retained take profit;
- a move-to-64100 management action.

After execution assert:

```python
assert current_purposes == {"stop_loss", "backup_stop", "take_profit"}
assert current_roles == {"primary_stop", "backup_stop", "take_profit"}
assert active_backup.trigger_price == "64100"
assert revision_is_complete is True
```

Also assert old role rows and the old backup order are terminal.

**Step 2: Run and verify failure**

```bash
uv run pytest \
  tests/test_strategy_management_executor.py \
  tests/test_position_mutation_gateway.py \
  tests/test_position_protection_legs.py -q
```

Expected: FAIL because the legacy management path persists both replacement
stops as `stop_loss` and leaves the old backup-role tables unchanged.

**Step 3: Extract one role-aware persistence helper**

Create a helper with an explicit closed input:

```python
@dataclass(frozen=True)
class VerifiedProtectionReplacement:
    role: Literal["primary_stop", "backup_stop", "take_profit"]
    order_id: str
    trigger_price: str
    size_text: str | None
```

The helper must receive the exact binding, entry leg, `posId`, instrument,
side, replacement batch/component identity, and verified rows. In one
transaction it updates the generic ledger, protection role rows, active backup
row, and complete revision. It must compare old values before changing them and
remain idempotent for the same exchange IDs.

**Step 4: Reuse the mutation gateway's role identity**

Pass `ledger_purpose="stop_loss"` for primary and
`ledger_purpose="backup_stop"` for backup at submission time. Reuse the
composite executor's create-new/read-back-before-cancel ordering in the legacy
move-stop path. Do not classify roles from price or returned row order.

**Step 5: Add refusal and restart tests**

Cover:

- missing backup read-back keeps old stops and returns a non-success state;
- duplicate new order IDs require operator action;
- restart after one new stop uses durable intent/read-back and does not resubmit
  blindly;
- old cancellation unknown leaves both new verified stops and enters
  `recovery_required`;
- repeated reconciliation creates no duplicate ledger, role, backup, or
  revision rows.

**Step 6: Run focused tests**

```bash
uv run pytest \
  tests/test_strategy_management_executor.py \
  tests/test_position_mutation_gateway.py \
  tests/test_protection_ledger.py \
  tests/test_position_protection_legs.py \
  tests/test_protection_revisions.py -q
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/protection_replacement_persistence.py \
  src/telegram_kol_research/strategy_management_executor.py \
  src/telegram_kol_research/strategy_management_composite_executor.py \
  src/telegram_kol_research/position_mutation_gateway.py \
  tests/test_strategy_management_executor.py \
  tests/test_position_mutation_gateway.py \
  tests/test_protection_ledger.py \
  tests/test_position_protection_legs.py \
  tests/test_protection_revisions.py
git commit -m "fix: persist complete protection replacement roles"
```

### Task 6: Converge protection incidents after verified replacement

**Files:**
- Modify: `src/telegram_kol_research/protection_health.py`
- Modify: `src/telegram_kol_research/protection_snapshot.py`
- Modify: `src/telegram_kol_research/runtime_incident_adapters.py`
- Modify: `tests/test_protection_snapshot.py`
- Modify: `tests/test_protection_ledger.py`
- Modify: `tests/test_runtime_incident_adapters.py`

**Step 1: Write failing current-health tests**

Persist historical `backup_stop_blocked` and `protection_missing` incidents,
then persist a newer complete verified primary/backup/take-profit replacement.
Assert the current audit is protected and does not retain the historical reason
as an active freeze.

Add a control case where the new backup lacks exact read-back; historical
freeze remains active.

**Step 2: Run and verify failure**

```bash
uv run pytest \
  tests/test_protection_snapshot.py \
  tests/test_protection_ledger.py \
  tests/test_runtime_incident_adapters.py -q
```

Expected: FAIL because the audit currently treats all historical incident rows
as current freeze reasons.

**Step 3: Add additive recovery evidence**

Do not delete or overwrite incident history. Append an exact convergence record
or expose a current-health projection that proves the replacement revision is
newer and complete for the same binding, leg, and `posId`. Only exact verified
current roles may supersede an earlier transient freeze in the projection.

**Step 4: Prevent stale runtime-incident re-alerting**

The durable source scanner must classify a recovered source as resolved rather
than opening another severe incident generation. Unknown or incomplete evidence
remains pending.

**Step 5: Run focused tests and commit**

```bash
uv run pytest \
  tests/test_protection_snapshot.py \
  tests/test_protection_ledger.py \
  tests/test_runtime_incident_adapters.py -q

git add src/telegram_kol_research/protection_health.py \
  src/telegram_kol_research/protection_snapshot.py \
  src/telegram_kol_research/runtime_incident_adapters.py \
  tests/test_protection_snapshot.py \
  tests/test_protection_ledger.py \
  tests/test_runtime_incident_adapters.py
git commit -m "fix: resolve transient protection incidents by evidence"
```

Expected: tests PASS and the commit succeeds.

### Task 7: Add a bounded read-only historical protection-incident audit

**Files:**
- Create: `src/telegram_kol_research/protection_incident_convergence.py`
- Modify: `src/telegram_kol_research/cli.py`
- Create: `tests/test_protection_incident_convergence.py`
- Modify: `tests/test_cli_smoke.py`
- Modify: `docs/runbook.md`

**Step 1: Write classifier tests**

Create cases for:

- `resolved_by_current_exchange_evidence`;
- `current_risk`;
- `historical_terminal`;
- `evidence_insufficient`.

Assert live exact positions are processed before history and that incomplete
snapshots never produce a resolved classification.

**Step 2: Write CLI safety tests**

Add `audit-protection-incidents` as read-only by default. Its JSON output must
be bounded and exclude raw messages, prompts, exchange payloads, order request
bodies, errors, and credentials. Assert the command has no `--apply` option in
this task.

**Step 3: Run and verify failure**

```bash
uv run pytest \
  tests/test_protection_incident_convergence.py \
  tests/test_cli_smoke.py -q
```

Expected: FAIL because the classifier and command do not exist.

**Step 4: Implement the read-only classifier**

Use an immutable/private SQLite snapshot following the existing bounded audit
pattern. Consume a coherent read-only Deepcoin snapshot supplied by the caller.
Return counts plus at most 100 redacted incident references. Set
`output_complete=false` on truncation, unstable database evidence, or exchange
read failure.

**Step 5: Document review rules**

In `docs/runbook.md`, state that the first production run sends no notification
and writes no business row. An operator must review all `current_risk` items
before any later incident-generation or selector change.

**Step 6: Run focused tests and commit**

```bash
uv run pytest \
  tests/test_protection_incident_convergence.py \
  tests/test_cli_smoke.py -q

git add src/telegram_kol_research/protection_incident_convergence.py \
  src/telegram_kol_research/cli.py \
  tests/test_protection_incident_convergence.py \
  tests/test_cli_smoke.py docs/runbook.md
git commit -m "feat: audit protection incident convergence"
```

Expected: tests PASS and the command remains side-effect free.

### Task 8: Add dormant scanner rules for missed management and role gaps

**Files:**
- Modify: `src/telegram_kol_research/config.py`
- Modify: `src/telegram_kol_research/runtime_incident_scanner.py`
- Modify: `src/telegram_kol_research/runtime_incident_rules.py`
- Modify: `tests/test_runtime_incident_scanner.py`
- Modify: `tests/test_runtime_incident_rules.py`
- Modify: `tests/test_runtime_agent_architecture_boundary.py`
- Modify: `docs/runtime-incident-agent-runbook.md`
- Modify: `docs/runtime-incident-agent-status.md`

**Step 1: Write pure-rule tests**

Add closed rules for:

- terminal high-risk management recognition with no executable instruction;
- verified protection replacement with an incomplete primary/backup role set.

Each rule must consume only bounded projections and return normal, abnormal, or
evidence-insufficient.

**Step 2: Write dormant-default and boundary tests**

Assert neither rule is deployable or enabled by default, the scanner writes
only observation metadata, and no scanner module imports strategy planners,
executors, Deepcoin clients, or Telegram senders.

**Step 3: Run and verify failure**

```bash
uv run pytest \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_rules.py \
  tests/test_runtime_agent_architecture_boundary.py -q
```

Expected: FAIL because the rule IDs and projections do not exist.

**Step 4: Implement pure dormant rules**

Add the rules to the reviewed catalog but not to
`RUNTIME_SCANNER_DEPLOYABLE_RULE_IDS`. Build no production projection until a
later separately approved runtime phase. Update the status file without
advancing more than the single approved phase.

**Step 5: Run tests and commit**

```bash
uv run pytest \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_rules.py \
  tests/test_runtime_incident_observations.py \
  tests/test_runtime_agent_architecture_boundary.py -q

git add src/telegram_kol_research/config.py \
  src/telegram_kol_research/runtime_incident_scanner.py \
  src/telegram_kol_research/runtime_incident_rules.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_runtime_incident_rules.py \
  tests/test_runtime_agent_architecture_boundary.py \
  docs/runtime-incident-agent-runbook.md \
  docs/runtime-incident-agent-status.md
git commit -m "feat: define dormant position compliance rules"
```

Expected: tests PASS; production behavior remains unchanged.

### Task 9: Run cross-component regression and review

**Files:**
- Modify as required only when a failing regression proves a defect in Tasks
  1-8.

**Step 1: Run the focused cross-component suite**

```bash
uv run pytest \
  tests/test_authoritative_recognition.py \
  tests/test_context_resolution.py \
  tests/test_context_resolution_worker.py \
  tests/test_message_recognition.py \
  tests/test_management_scope.py \
  tests/test_management_directives.py \
  tests/test_strategy_management_executor.py \
  tests/test_position_mutation_gateway.py \
  tests/test_protection_ledger.py \
  tests/test_protection_snapshot.py \
  tests/test_runtime_incident_adapters.py \
  tests/test_runtime_incident_scanner.py \
  tests/test_telegram_live_listener.py \
  tests/test_web_app.py -q
```

Expected: PASS.

**Step 2: Run the full suite**

```bash
uv run pytest tests -q
```

Expected: PASS with only already documented skips/warnings.

**Step 3: Inspect scope and safety**

```bash
git status --short
git diff --check
git log --oneline --decorate -12
```

Expected: only reviewed task files are modified; no credentials, runtime data,
media, database, audit artifacts, or user-owned unrelated changes are staged.

**Step 4: Request code review**

Use the `requesting-code-review` skill. Review specifically for:

- cross-chat or cross-lifecycle fanout;
- partial persistence before full target validation;
- retry after an unknown exchange write;
- notification dependence in retry ownership;
- protection-role inference from price/order proximity;
- historical notification storms;
- Runtime Agent business-write authority.

**Step 5: Apply only reviewed fixes and rerun affected tests**

Expected: no Critical or Important findings remain.

### Task 10: Push reviewed commits

**Files:** None unless review fixes are required.

**Step 1: Verify branch and remote divergence**

```bash
git branch --show-current
git status --short --branch
git log --oneline origin/codex/deepcoin-auto-trading-v1..HEAD
```

Expected: branch is `codex/deepcoin-auto-trading-v1`; only intentional commits
are ahead.

**Step 2: Push**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: push succeeds without force.

### Task 11: Deploy notification and context-failure capture stage

**Files:**
- Modify only if server verification reveals a code defect; do not edit
  production data.

**Step 1: Prove a safe deployment window with read-only checks**

On the server confirm:

- service active and current SHA known;
- no context/evidence claim in flight;
- no management batch executing or awaiting an unknown submission;
- no position mutation or recovery attempt in flight;
- complete current Deepcoin positions and pending TPSL snapshot;
- Telegram listener checkpoint advancing.

Expected: every check is complete and zero time-sensitive work is active.

**Step 2: Deploy through the existing helper**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

Expected: the server pulls the reviewed branch, reinstalls the editable
package, restarts `telegram-kol.service`, and reports active.

**Step 3: Keep new runtime selectors unchanged**

Do not add new capture, Telegram, or Agent types in the same deployment turn.
Do not enable scanner rules.

**Step 4: Verify natural continuity**

Confirm server SHA, service PID/start time, HTTP 200, listener group count,
latest natural recognition, zero in-flight management/mutations, and current
exchange read-back. Run focused deployed tests for Tasks 1-2.

Expected: normal paths remain healthy; no historical notification is emitted.

### Task 12: Canary later runtime stages in separate approved turns

**Files:**
- Modify: `docs/runtime-incident-agent-status.md` after each separately approved
  canary.

**Step 1: Multi-target shadow canary**

Use the redacted message-3465 fixture and current code in read-only/shadow mode.
Expected: two exact target plans, zero management batches, zero Deepcoin writes.

**Step 2: Enable multi-target live only for new natural messages**

Require a new explicit approval and a fresh safe-window check. Never replay the
fixture or production message 3465.

**Step 3: Protection-role shadow/read-back canary**

Verify role-aware persistence against a naturally occurring protection update
only. Do not create a test update. Expected: exact primary/backup/take-profit
roles and no active stale freeze.

**Step 4: Run the historical audit with no notification**

```bash
.venv/bin/telegram-kol-research audit-protection-incidents \
  --database-path data/research.db --limit 100 --output-format json
```

Expected: complete bounded classification or an explicit incomplete result;
zero source-row mutation and zero Telegram delivery.

**Step 5: Enable one deterministic incident type**

After reviewing only current risks, add one exact type to the Telegram selector
in a separate turn. Verify one new-generation canary and no historical storm.

**Step 6: Enable read-only Agent diagnosis for the same proven type**

In another separate turn, add the exact type to the Agent selector. Keep
actions false and playbook allowlists empty. Verify the diagnosis references
only bounded evidence and reports `action_executed: false`.

**Step 7: Update the canonical status file**

Record deployed SHA, service state, focused/full tests, canary evidence,
selector values, notification count, Agent result, action authority, rollback
state, and any remaining work.

**Step 8: Commit and push the status update**

```bash
git add docs/runtime-incident-agent-status.md
git commit -m "docs: record position compliance canary"
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub and production handoff evidence agree.

