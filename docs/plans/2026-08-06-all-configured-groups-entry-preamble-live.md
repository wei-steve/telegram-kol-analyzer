# All Configured Groups Entry-Preamble Live Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make live entry-preamble sizing apply to every configured trading group without a per-chat allowlist, then publish the reviewed recognition prompt and activate production live mode safely.

**Architecture:** Keep the existing durable recognition, source-order assembly, and fail-closed evidence chain. Change only the live authorization boundary: `entry_preamble_mode=live` authorizes every group already admitted by the configured trading-group pipeline. Preserve `disabled` as the rollback default, retain `shadow` internally as test-only compatibility, and remove the obsolete chat allowlist from execution and the operator form.

**Tech Stack:** Python 3.12+, SQLAlchemy/SQLite, FastAPI, Jinja2/JavaScript, pytest, versioned AI prompt registry, systemd, Deepcoin execution adapters.

---

### Task 1: Make live mode apply to every configured trading group

**Files:**
- Modify: `tests/test_entry_strategy_assembly.py`
- Modify: `src/telegram_kol_research/entry_strategy_assembly.py:378-470`

**Step 1: Write the failing tests**

Replace allowlist-dependent expectations with two explicit cases:

```python
def test_live_assembly_applies_to_configured_chat_without_allowlist(tmp_path):
    session_factory = create_session_factory(tmp_path / "all-groups.db")
    strategy_raw_id, candidate_id, _ = _persist_pair(session_factory)

    result = assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id="configured-group-entry",
        mode="live",
        assembled_at=NOW + timedelta(minutes=2),
    )

    assert result.status == "assembled"
    assert result.effective_risk_multiplier == Decimal("0.5")


def test_non_live_retry_blocks_existing_live_assembly(tmp_path):
    # Preserve the existing disabled/shadow downgrade regression.
    ...
```

Update every direct call to remove `live_chat_ids`.

**Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_entry_strategy_assembly.py
```

Expected: FAIL because `assemble_entry_strategy` still requires and checks `live_chat_ids`.

**Step 3: Implement the minimal authorization change**

Remove `live_chat_ids` from `assemble_entry_strategy`. Change the existing-assembly guard to:

```python
if existing is not None:
    if mode != "live":
        return EntryAssemblyResult(
            status="blocked",
            reason_code="existing_entry_assembly_not_live_authorized",
            mode=mode,
            proposed_risk_multiplier=Decimal("1"),
            effective_risk_multiplier=Decimal("1"),
            strategy_message_id=int(strategy_message.message_id),
        )
    ...
```

For a new ready assembly, `mode == "live"` consumes it regardless of chat ID.
Do not weaken source-order, ambiguity, evidence-version, or multiplier checks.

**Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_entry_strategy_assembly.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_entry_strategy_assembly.py src/telegram_kol_research/entry_strategy_assembly.py
git commit -m "feat: apply live preambles to all configured groups"
```

### Task 2: Apply the all-group rule at the real exchange-write boundary

**Files:**
- Modify: `tests/test_auto_trade_execution.py:780-930`
- Modify: `src/telegram_kol_research/auto_trade_execution.py:570-615`

**Step 1: Write the failing integration tests**

Change the live half-risk test so the settings contain no chat allowlist:

```python
save_trading_settings(
    session_factory,
    {
        "auto_trade_enabled": True,
        "default_max_loss_usdt": 20,
        "allowed_symbols": ["BTC"],
        "entry_preamble_mode": "live",
    },
)
```

Assert that the submitted draft and binding still use 10 USDT. Add a second
configured chat with the same settings and assert it also uses 10 USDT. Keep an
ordinary no-preamble entry assertion at 20 USDT. Preserve the tests proving
unresolved and ambiguous preambles make zero Deepcoin write calls.

**Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_auto_trade_execution.py -k "entry_preamble or half_risk"
```

Expected: FAIL because the current call passes an empty allowlist and therefore does not apply the multiplier.

**Step 3: Update the execution call**

Change the call to:

```python
assembly = assemble_entry_strategy(
    session_factory,
    strategy_raw_message_id=int(raw_message.id),
    signal_candidate_id=int(candidate.id),
    strategy_instance_id=strategy_instance_id,
    mode=settings.entry_preamble_mode,
    assembled_at=now,
)
```

Do not change the ordering of `configured_risk * effective_risk_multiplier`;
it must remain before draft construction and contract quantity calculation.

**Step 4: Run tests to verify they pass**

Run:

```bash
.venv/bin/pytest -q tests/test_auto_trade_execution.py -k "entry_preamble or half_risk"
```

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_auto_trade_execution.py src/telegram_kol_research/auto_trade_execution.py
git commit -m "feat: size all configured groups from live preambles"
```

### Task 3: Remove the obsolete allowlist from settings and operator UI

**Files:**
- Modify: `tests/test_trading_settings.py:35-80`
- Modify: `tests/test_web_app.py:1170-1210`
- Modify: `tests/test_web_page_render.py:710-735`
- Modify: `src/telegram_kol_research/trading_settings.py:60-80,280-315,450-470`
- Modify: `src/telegram_kol_research/templates/index.html:350-370`
- Modify: `src/telegram_kol_research/static/app.js:2835-2845`

**Step 1: Write the failing settings and UI tests**

Assert that:

```python
assert not hasattr(settings, "entry_preamble_live_chat_ids")
assert 'name="entry_preamble_live_chat_ids"' not in response.text
assert "实盘：所有已配置交易群组" in response.text
assert "测试：只记录，不改变真实下单" in response.text
```

Update the API round-trip test so a legacy `entry_preamble_live_chat_ids`
input is ignored and omitted from the response instead of controlling execution.

**Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest -q tests/test_trading_settings.py tests/test_web_app.py tests/test_web_page_render.py
```

Expected: FAIL on the still-present field and old UI wording.

**Step 3: Implement backward-compatible removal**

Remove the dataclass field, parser, serializer, form input, and JavaScript
payload field. Do not delete legacy SQLite JSON keys; simply stop projecting or
consulting them. Keep mode validation restricted to `disabled`, `shadow`, and
`live`. Use these operator labels:

```html
<option value="disabled">关闭：不使用之前的仓位提示</option>
<option value="shadow">测试：只记录，不改变真实下单</option>
<option value="live">实盘：所有已配置交易群组</option>
```

**Step 4: Run tests to verify they pass**

Run the same command. Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_trading_settings.py tests/test_web_app.py tests/test_web_page_render.py src/telegram_kol_research/trading_settings.py src/telegram_kol_research/templates/index.html src/telegram_kol_research/static/app.js
git commit -m "refactor: remove entry preamble chat allowlist"
```

### Task 4: Align documentation and monitoring expectations

**Files:**
- Modify: `docs/runbook.md:575-620`
- Modify: `docs/entry-preamble-live-verification.md`
- Modify: `tests/test_production_safety_monitor.py`
- Verify: `src/telegram_kol_research/production_safety_monitor.py`

**Step 1: Add/adjust the failing monitor regression**

Add a test with entry-preamble assemblies from two different configured chats
and no allowlist setting. Assert no configuration-drift or evidence-missing
reason occurs when both bindings contain matching fingerprints. Preserve stale,
ambiguous, and missing-binding evidence tests.

**Step 2: Run the monitor tests**

```bash
.venv/bin/pytest -q tests/test_production_safety_monitor.py
```

Expected: PASS if no monitor code depends on the allowlist; otherwise FAIL and
identify the minimal compatibility change.

**Step 3: Update operator documentation**

Replace selected-chat rollout instructions with all-configured-group live
semantics. State plainly that production live mode changes real new-order risk,
ordinary entries are unchanged, and rollback is `entry_preamble_mode=disabled`.
Remove instructions to populate `entry_preamble_live_chat_ids`.

**Step 4: Run tests and inspect documentation**

```bash
.venv/bin/pytest -q tests/test_production_safety_monitor.py
rg -n "entry_preamble_live_chat_ids|selected chats|allowlist" docs/runbook.md docs/entry-preamble-live-verification.md
```

Expected: monitor tests PASS and no obsolete activation instruction remains.

**Step 5: Commit**

```bash
git add docs/runbook.md docs/entry-preamble-live-verification.md tests/test_production_safety_monitor.py src/telegram_kol_research/production_safety_monitor.py
git commit -m "docs: document all-group preamble live operation"
```

### Task 5: Verify the production prompt publication package

**Files:**
- Modify if needed: `tests/test_prompt_registry.py`
- Verify: `src/telegram_kol_research/prompt_defaults.py:28-105`
- Verify: `src/telegram_kol_research/message_evidence.py:300-340`

**Step 1: Strengthen the prompt contract test**

Assert the reviewed default explicitly contains:

```python
assert '"entry_context"' in prompt
assert "半仓" in prompt
assert "risk_multiplier = 0.5" in prompt
assert "最大亏损预算" in prompt
assert "轻仓" in prompt
assert "不得猜测倍率" in prompt
```

Also retain the null-strategy, complete-strategy stray context, management
message, and malformed evidence regressions.

**Step 2: Run prompt and recognition tests**

```bash
.venv/bin/pytest -q tests/test_prompt_registry.py tests/test_message_evidence.py tests/test_authoritative_recognition.py tests/test_entry_preambles.py
```

Expected: PASS, or a narrowly scoped prompt wording failure to fix.

**Step 3: Make only required prompt wording changes**

Do not auto-overwrite the production registry. Modify the default only if the
contract test exposes missing reviewed wording.

**Step 4: Re-run tests**

Run the same command. Expected: PASS.

**Step 5: Commit if files changed**

```bash
git add tests/test_prompt_registry.py src/telegram_kol_research/prompt_defaults.py
git commit -m "test: lock all-group preamble prompt contract"
```

### Task 6: Review and complete local verification

**Files:**
- Review: all changes since `3c28399`

**Step 1: Run focused coverage**

```bash
.venv/bin/pytest -q \
  tests/test_entry_preambles.py \
  tests/test_entry_strategy_assembly.py \
  tests/test_authoritative_recognition.py \
  tests/test_auto_trade_execution.py \
  tests/test_trading_settings.py \
  tests/test_web_app.py \
  tests/test_web_page_render.py \
  tests/test_production_safety_monitor.py \
  tests/test_prompt_registry.py
```

Expected: PASS.

**Step 2: Run the full suite**

```bash
.venv/bin/pytest -q
```

Expected: all feature tests pass. Record the two known unrelated baseline
failures separately if they remain:

- `tests/test_cli_smoke.py::test_monitor_production_prints_compact_fixed_summary_and_exits_nonzero`
- `tests/test_server_monitor_installation.py::test_installer_creates_identity_and_allowlisted_monitor_environment`

Do not modify unrelated files merely to hide baseline failures.

**Step 3: Request code review**

Review the diff for exchange-write authorization, rollback, source-order gaps,
prompt compatibility, monitoring correctness, and missing tests. Address every
Critical or Important finding with a failing regression test first.

**Step 4: Confirm worktree scope**

```bash
git diff --check
git status --short
git log --oneline 3c28399..HEAD
```

Expected: only intended tracked files are committed; user-owned `uv.lock` and
inspection artifacts remain untouched.

### Task 7: Push, deploy dormant, publish prompt, and activate all groups live

**Files:**
- Use: `scripts/server_git_update.sh`
- Use: `docs/entry-preamble-live-verification.md`

**Step 1: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: remote branch advances to reviewed HEAD.

**Step 2: Prove a safe deployment window**

On the server, read only bounded counts for active management batches,
submitted mutation intents, recovery claims, latest incoming message time, and
current service state. Stop if any time-sensitive strategy operation is active.

**Step 3: Deploy with the existing switch still disabled**

```bash
./scripts/server_git_update.sh
```

Expected: fast-forward pull, editable reinstall, successful service restart,
and `entry_preamble_mode=disabled` immediately after restart.

**Step 4: Run server-focused tests and invariant checks**

```bash
.venv/bin/pytest -q \
  tests/test_entry_preambles.py \
  tests/test_entry_strategy_assembly.py \
  tests/test_auto_trade_execution.py \
  tests/test_trading_settings.py \
  tests/test_production_safety_monitor.py
```

Expected: PASS, service active, zero new error-priority journal entries, and
`read_entry_preamble_invariants(...) == ()`.

**Step 5: Publish the production prompt safely**

Use the Web prompt center for `trading.analysis.shared`:

1. Load the current active version.
2. Save the reviewed default as a draft with a non-empty change note.
3. Validate the draft.
4. Run current historical comparisons for both MiMo and DeepSeek using curated
   raw-message IDs that cover preamble, ordinary strategy, management, vague
   sizing, and malformed/empty strategy output.
5. Confirm both runs complete against the current active prompt versions.
6. Publish with optimistic active/draft version IDs.

Expected: the new active prompt contains the exact `entry_context` contract;
no prompt is published if validation or either historical model run fails.

**Step 6: Recheck the safe activation window**

Repeat the bounded read-only in-flight checks. Stop if any new operation is
active.

**Step 7: Activate live mode for all configured groups**

Update only:

```json
{"entry_preamble_mode": "live"}
```

Preserve all other trading settings. Read the settings back and confirm live
mode. Do not populate any chat allowlist.

**Step 8: Verify activation without submitting a synthetic exchange order**

Confirm the service remains active, the prompt version is current, monitor
invariants are empty, and the next real configured-group entry will use the
live all-group path. Do not fabricate Telegram normalized evidence and do not
replay an old strategy into the exchange path.

**Step 9: Record the handoff**

Report the deployed commit, prompt version, production mode, focused test
results, monitor result, and immediate rollback instruction:

```text
Set entry_preamble_mode=disabled.
```
