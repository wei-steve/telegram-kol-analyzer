# Phase 2 — Per-Chat Lock Sharding

> Self-contained. Do not read other phase files in this session.
> Parent design: `docs/plans/2026-08-18-runtime-serialization-remediation-design.md`
> Line anchors in this file were verified against commit `2fc0ad2`. If a line
> number no longer matches, the symbol name next to it is authoritative — search
> for that instead of trusting the number.

**Goal:** Stop one group's slow message from delaying every other group's
messages, by replacing the single process-global message lock with per-chat
locks.

**Nature:** Concurrency change. This is the phase that directly fixes "messages
from certain groups do not produce trades". It is also the highest-risk phase so
far, because it genuinely introduces parallelism that did not exist before.

**Prerequisite:** Phase 1 is complete and deployed. Do not attempt this phase
while the event loop can still be blocked — the two effects would be
indistinguishable in production.

## Why this phase exists

`handle_new_message` holds one process-wide `asyncio.Lock` across the entire
processing chain:

`src/telegram_kol_research/telegram_live_listener.py:624`

```python
if operation_lock is None:
    await persist_live_message_event(**persist_kwargs)
else:
    async with operation_lock:
        await persist_live_message_event(**persist_kwargs)
```

The lock is created once at `src/telegram_kol_research/web_app.py:4592` and is
held across: persist, media download, OCR, MiMo recognition (LLM, timeout capped
at 120s in `src/telegram_kol_research/llm_chat.py:171`), contextual resolution,
`auto_trade_executor`, and Deepcoin order submission (15s per call).

The same lock is taken by:

- `handle_deleted_message` (`telegram_live_listener.py:661`)
- `run_periodic_reconcile` (`telegram_live_listener.py:1091`), which holds it
  while replaying up to 50 messages through the full chain, every 300 seconds
- the manual refresh endpoints (`web_app.py:6783`, `web_app.py:7457`)

So a slow image message in group A delays every message in group B by the full
LLM plus exchange round trip, and a reconcile pass delays live messages for its
entire duration. The instruction is not misrecognized — it is never reached in
time.

## Why per-chat is the correct granularity

Within one chat, message order carries meaning: replies, edits, follow-up
management instructions, and the adjacent-entry assembly logic all depend on
messages from the same chat being processed in arrival order. That ordering must
be preserved.

Across chats there is no shared mutable state that requires exclusion at this
layer. The state that genuinely requires exclusion is per-position, and it
already has its own boundary:
`src/telegram_kol_research/position_authority_lock.py` provides a process-wide
`RLock` via `position_authority_lock()` and
`serialized_position_authority_mutation`.

So: per-chat serialization preserves every ordering guarantee that currently
matters, and `position_authority_lock` continues to protect the exchange
mutation boundary. That layering is the entire safety argument for this phase and
must be verified in Task 1 before any code changes.

## Scope

Introduce a keyed async lock registry, key the live message and deletion paths by
`chat_id`, and make the reconcile and manual refresh paths acquire the same
per-chat locks rather than a global one. Ship behind a flag defaulting to the
current global behavior.

Out of scope: parallelism within a chat, and any change to recognition or
execution logic.

### Task 1: Verify the position authority boundary actually covers exchange mutation

**Do this before writing any code. If it fails, stop and report rather than
proceeding.**

Enumerate every path reachable from `auto_trade_executor`
(`src/telegram_kol_research/web_app.py:3537` →
`auto_process_message_trade_signal` in
`src/telegram_kol_research/auto_trade_execution.py`) that mutates position or
protection state or submits an exchange order, and confirm each one is inside
`position_authority_lock()` or decorated with
`serialized_position_authority_mutation`.

**Files:**
- Create: `tests/test_position_authority_boundary_coverage.py`

Write an architecture test that asserts this coverage, so the guarantee is
enforced rather than assumed. Where coverage is genuinely absent, record the
exact gap in the status file.

**Decision gate:** if any exchange mutation path reachable from two different
chats concurrently is not covered by `position_authority_lock`, do not enable
per-chat sharding in this phase. Finish Tasks 2 and 3, leave the flag disabled,
record the gap, and stop. Closing that gap is its own phase.

### Task 2: Add the keyed lock registry

**Files:**
- Create: `src/telegram_kol_research/keyed_async_locks.py`
- Create: `tests/test_keyed_async_locks.py`

Provide `KeyedAsyncLockRegistry` with:

- `def lock(self, key: Hashable) -> AbstractAsyncContextManager[None]` returning
  a per-key `asyncio.Lock` context manager, created on first use.
- `async def lock_all(self) -> AbstractAsyncContextManager[None]` acquiring every
  currently known key's lock in a deterministic sorted order, for the rare
  cross-chat operations. Deterministic order is what prevents deadlock; assert it
  in a test.
- Bounded growth: never let the registry grow without limit across a long uptime.
  Either cap it, or drop entries whose lock is unlocked and unreferenced. Assert
  the bound in a test.

Tests must cover: two different keys proceed concurrently, the same key
serializes, ordering within a key is FIFO, `lock_all` is deadlock-free under
concurrent per-key acquisition, and the registry stays bounded.

[local]

```bash
.venv/bin/python -m pytest tests/test_keyed_async_locks.py -v
```

### Task 3: Add the flag and thread the registry through

**Files:**
- Modify: `src/telegram_kol_research/trading_settings.py`
- Modify: `src/telegram_kol_research/telegram_live_listener.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_trading_settings*.py`, `tests/test_web_app.py`

**Step 1 — Add the setting**

Add `message_lock_mode: Literal["global", "per_chat"] = "global"` to
`TradingSettings` (`src/telegram_kol_research/trading_settings.py:63` onward has
the existing flag block; match its style). Default `global` preserves today's
behavior exactly.

**Step 2 — Introduce a lock provider instead of a bare lock**

Replace the `operation_lock` parameter with a provider that resolves a context
manager from a `chat_id`. In `global` mode it returns the existing single lock
for every chat, so the code path is identical to today. In `per_chat` mode it
returns the keyed registry's per-chat lock.

Keep the parameter name and the `None` fallback so existing tests and callers do
not break.

**Step 3 — Key the live paths**

`handle_new_message` (`:600`) and `handle_deleted_message` (`:630`) both already
have `chat_id` available. Acquire by `chat_id`.

**Step 4 — Key the reconcile path**

`run_periodic_reconcile` (`:1053`) currently wraps the whole
`run_reconcile_once` in the global lock. In `per_chat` mode it must instead
acquire the per-chat lock **around each chat's work**, and must not hold any lock
while fetching dialogs from Telegram.

This is the change that stops a reconcile pass from freezing live traffic. It is
also the subtlest part of this phase — the recovery loop at
`telegram_live_listener.py:840` iterates messages across chats, so the lock has to
be taken per message's `chat_id`, inside the loop, not around it.

**Step 5 — Key the manual refresh endpoints**

`web_app.py:6783` and `web_app.py:7457` take the global lock. In `per_chat` mode
these are cross-chat operations, so they use `lock_all()`.

### Task 4: Concurrency regression tests

**Files:**
- Create: `tests/test_live_listener_chat_isolation.py`

Assert, with injected fakes and no real network:

1. In `per_chat` mode, a slow message in chat A does not delay a message in chat
   B — chat B completes while chat A is still in flight.
2. In `per_chat` mode, two messages in the same chat are still processed in
   arrival order, one at a time.
3. In `global` mode, behavior is byte-for-byte the old behavior.
4. A reconcile pass in `per_chat` mode does not block a live message in a chat it
   is not currently processing.

Test 1 and test 4 are the ones that encode the actual bug being fixed.

### Task 5: Full local suite and commit

[local]

```bash
.venv/bin/python -m pytest -q
```

Expected: no new failures. The default is `global`, so the entire existing suite
must be unaffected.

[local]

**Never `git add -A`.** Other sessions may be working in this same checkout, and
`-A` sweeps their unfinished work into your commit. Stage the exact paths this
phase touched, and check what you staged before committing:

```bash
git status --short
git add src/telegram_kol_research/keyed_async_locks.py \
  src/telegram_kol_research/telegram_live_listener.py \
  src/telegram_kol_research/trading_settings.py \
  src/telegram_kol_research/web_app.py \
  tests/test_keyed_async_locks.py \
  tests/test_live_listener_chat_isolation.py \
  tests/test_position_authority_boundary_coverage.py
git diff --cached --name-only
git commit -m "feat: add per-chat message lock sharding behind a dormant flag"
```

If `git diff --cached --name-only` lists anything this phase did not touch,
unstage it with `git restore --staged <path>` before committing.

### Task 6: Deploy dormant, then enable

**Step 1 — Deploy with `message_lock_mode: global`**

**Deployment is a gated updater, not a manual pull.** Follow
`docs/plans/2026-08-18-runtime-serialization-remediation/deployment-procedure.md`.
This phase deploys with `-ChangeClass execution_writer`.

Because the class is `execution_writer`, capture a prior independent live
position snapshot first and pass `-PreviousLiveSnapshotPath`.

[local] Commit, push to the deploy branch recorded as `deploy_branch` in the
status file, confirm the commit is on the remote, then run
`scripts/server_git_update.ps1` with that 40-hex SHA and the change class above.

The updater enforces the safe window itself through `deployment-preflight`
before it stops the service. If it returns `BLOCK`, read the reason, wait, and
record it — do not retry blindly.

Confirm the system behaves exactly as before and that `message_lock_mode` reads
`global`.

**Step 2 — Prove the disable path before enabling**

Flip to `per_chat`, confirm it takes effect, flip back to `global`, confirm it
takes effect. The rollback must be proven working before the phase is allowed to
stay enabled.

**Step 3 — Enable `per_chat` and observe**

Enable it during a period with real multi-group traffic. Observe for at least one
full trading session:

- Messages in different groups are recognized concurrently.
- Within each group, ordering is preserved.
- No duplicate orders, no position attribution errors, no protection ledger
  incidents that did not occur before.
- Loop lag from the Phase 0 endpoint remains healthy.

**Step 4 — What to watch for specifically**

The new risk in this phase is two chats reaching the exchange at the same moment.
Watch `position_attribution_audits`, `position_protection_incidents`, and
`execution_events` for any new class of row that did not appear under `global`.
If one appears, disable immediately by setting `message_lock_mode: global` — no
redeploy required — and record it.

## Completion criteria

- Task 1's boundary verification passed, or its gap is recorded and the flag was
  deliberately left disabled.
- Chat isolation and same-chat ordering tests pass.
- `per_chat` enabled in production, with the disable path proven working
  beforehand.
- One full trading session observed with no new incident class.

## Rollback

Set `message_lock_mode: global` in trading settings. Immediate, no restart and no
deploy — `deployment-procedure.md` rollback level 1, and always the first thing
to reach for.

If the code itself must go, redeploy the previous known good 40-hex SHA with
`-ChangeClass execution_writer`. There is no database migration in this phase.

## Status file update

Set `phase_status: completed`, `current_phase: 3`,
`phase_name: compensation-window-repair`,
`current_phase_file: docs/plans/2026-08-18-runtime-serialization-remediation/phase-3-compensation-window-repair.md`.
Append `local_tests` and `server_verification` entries. Record explicitly whether
`per_chat` is enabled in production or was left dormant, and why.
