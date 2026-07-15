# Equivalent Entry-Leg Attribution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent duplicate entry legs for one-price strategies and safely recover same-binding, economically equivalent historical position permutations.

**Architecture:** Normalize entry legs at draft construction and again at the live-submission boundary. Extend the pure attribution model with protection/equivalence signatures, but enable canonical equivalent-permutation assignment only in the audited repair planner so deployment cannot mutate production before dry-run review. Persist a versioned `equivalent_permutation_assignment` record so ordinary reconcile and live authority gates can safely inherit the reviewed result.

**Tech Stack:** Python 3.12+, SQLAlchemy, FastAPI, Typer, pytest, Deepcoin authenticated REST API.

---

### Task 1: Build one leg for a single normalized entry price

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py:23-230`
- Test: `tests/test_deepcoin_order_builder.py`

**Step 1: Write the failing single-limit-price test**

Add a test that calls `build_deepcoin_order_draft()` with `order_type="limit"`,
`entry_range="63700-63700"`, a valid BTC contract spec, stop loss, and risk
budget. Require exactly one order leg with `allocation_pct == 100.0`, the full
risk budget, one client order ID ending in entry leg 1 semantics, and the
quantity calculated from the full risk budget.

```python
def test_build_single_price_limit_entry_as_one_full_risk_leg():
    draft = build_deepcoin_order_draft(
        _payload_preview(entry_range="63700-63700", stop_loss="62500"),
        contract_spec=_btc_contract_spec(),
    )

    assert len(draft["order_legs"]) == 1
    assert draft["order_legs"][0]["price"] == 63700.0
    assert draft["order_legs"][0]["allocation_pct"] == 100.0
    assert draft["order_legs"][0]["risk_budget_usdt"] == 100.0
```

**Step 2: Run the test and verify the current duplicate-leg failure**

Run:

```bash
.venv/bin/pytest -q tests/test_deepcoin_order_builder.py::test_build_single_price_limit_entry_as_one_full_risk_leg
```

Expected: FAIL because the current limit branch returns two 50% legs.

**Step 3: Extract one-leg construction and use it for equal low/high**

Add a small helper that constructs the existing market-style 100% leg without
changing its requested order type:

```python
def _single_entry_leg(..., order_type: str, price: float, ...):
    return _order_leg(
        side=open_side,
        position_side=position_side,
        order_type=order_type,
        price=price,
        client_order_id=build_client_order_id(..., leg_index=1, ...),
        allocation_pct=100.0,
        risk_budget_usdt=risk_budget,
        quantity=_estimate_leg_quantity(
            risk_budget=risk_budget,
            allocation_pct=100.0,
            entry_price=price,
            stop_loss=stop_loss,
        ),
        stop_loss=stop_loss,
        contract_spec=contract_spec,
    )
```

After entry price normalization, branch on `isclose(entry_low, entry_high)`
before the hybrid/two-limit branches. Preserve the existing one-market-leg
behavior.

**Step 4: Add true-range regression tests**

Require `63300-63700` to retain two distinct normalized legs and its configured
range style. Also require a nearby single price converted to market by
`auto_trade_execution` to remain one leg.

**Step 5: Run builder and auto-trade tests**

Run:

```bash
.venv/bin/pytest -q tests/test_deepcoin_order_builder.py tests/test_auto_trade_execution.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/deepcoin_order_builder.py \
  tests/test_deepcoin_order_builder.py tests/test_auto_trade_execution.py
git commit -m "fix: build one leg for single price entries"
```

### Task 2: Coalesce range legs that collapse after exchange normalization

**Files:**
- Modify: `src/telegram_kol_research/deepcoin_order_builder.py`
- Test: `tests/test_deepcoin_order_builder.py`

**Step 1: Write the failing tick-collapse test**

Use a contract with a coarse `price_tick` so two distinct input range prices
normalize to the same final price. Require one output leg whose quantity,
allocation, risk budget, and estimated stop loss equal the sums of the two
pre-merge legs.

**Step 2: Run the focused test and verify it fails**

Run the exact new pytest node. Expected: FAIL with two final legs at the same
price.

**Step 3: Add an economic-identity merge helper**

Implement a pure `_coalesce_equivalent_entry_legs()` that groups only legs with
the same order type, normalized price, side, position side, and quantity unit.
Merge allocation/risk/quantity fields, retain the first stable client order ID,
and record `merged_from_leg_indices` or an equivalent draft note. Never merge
legs with different final prices or execution types.

**Step 4: Run builder tests**

```bash
.venv/bin/pytest -q tests/test_deepcoin_order_builder.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/deepcoin_order_builder.py \
  tests/test_deepcoin_order_builder.py
git commit -m "fix: merge normalized duplicate entry legs"
```

### Task 3: Enforce the one-equivalent-order invariant at live submission

**Files:**
- Modify: `src/telegram_kol_research/recovery_live_submit.py:225-460`
- Test: `tests/test_recovery_live_submit.py`

**Step 1: Write failing legacy-draft tests**

Construct a queued legacy draft containing two identical trigger-limit legs.
Require live submission to make one Deepcoin trigger request with combined
quantity and one persisted `ExecutionOrderLeg`. Add a negative test proving two
different prices still submit two requests.

**Step 2: Run the tests and verify duplicate submission**

Run the two exact pytest nodes. Expected: the legacy-equivalent test observes
two trigger requests.

**Step 3: Reuse the pure coalescing helper before submission**

Apply coalescing inside `_submission_order_legs()` or immediately before it.
Do not mutate the original queued payload in place. Persist the submitted leg's
merge metadata in its request/evidence payload for later audit.

**Step 4: Run live-submission and binding tests**

```bash
.venv/bin/pytest -q tests/test_recovery_live_submit.py tests/test_execution_bindings.py
```

Expected: PASS.

**Step 5: Commit**

```bash
git add src/telegram_kol_research/recovery_live_submit.py \
  tests/test_recovery_live_submit.py tests/test_execution_bindings.py
git commit -m "fix: block equivalent duplicate live entries"
```

### Task 4: Model entry and protection equivalence without granting authority

**Files:**
- Modify: `src/telegram_kol_research/position_attribution.py`
- Modify: `src/telegram_kol_research/execution_bindings.py:780-910`
- Test: `tests/test_position_attribution.py`
- Test: `tests/test_execution_bindings.py`

**Step 1: Write failing pure equivalence tests**

Add production-shaped tests for:

- two same-binding legs and two identical positions forming one closed 2x2
  component;
- the same graph spanning two bindings;
- unequal quantities, prices, TP, or SL;
- one terminal/cancelled leg;
- an outside candidate edge; and
- missing/API-failed evidence.

At this stage require the helper to classify only the first graph as eligible;
do not yet create assignments.

**Step 2: Run tests and verify the helper is missing**

Run the exact new nodes. Expected: FAIL.

**Step 3: Extend evidence records with normalized signatures**

Add optional immutable fields with safe defaults to `LegEvidence` and
`PositionEvidence`, for example:

```python
entry_price: float | None = None
stop_loss: float | None = None
take_profits: tuple[float, ...] = ()
margin_mode: str | None = None
position_mode: str | None = None
order_kind: str | None = None
```

Populate leg values from `request_json`, binding payload/draft, and binding
modes. Populate position values only from direct position fields such as
`avgPx`, `slTriggerPx`, `tpTriggerPx`, `mgnMode`, and `mrgPosition`.

**Step 4: Implement closed-component eligibility**

Create a pure function returning eligible connected components only when:

- all legs have one binding/strategy identity;
- leg and position counts are equal;
- every node's candidate edges stay inside the component;
- every leg is nonterminal and has successful fill/trigger evidence;
- normalized economic signatures are equivalent; and
- no authoritative outside owner exists.

Use explicit tolerance helpers rather than decimal-string equality. Keep this
function classification-only.

**Step 5: Add TP/SL mutual-unique filtering tests**

Prove that distinct directly reported position TP/SL can eliminate incompatible
edges and create a mutual-unique result. Prove equal or missing TP/SL cannot
break a tie. Prove a recorded post-entry protection mutation disables TP/SL as
entry identity evidence.

**Step 6: Run focused attribution tests**

```bash
.venv/bin/pytest -q tests/test_position_attribution.py tests/test_execution_bindings.py
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/position_attribution.py \
  src/telegram_kol_research/execution_bindings.py \
  tests/test_position_attribution.py tests/test_execution_bindings.py
git commit -m "feat: classify equivalent attribution components"
```

### Task 5: Add deterministic equivalent-permutation assignments to repair only

**Files:**
- Modify: `src/telegram_kol_research/position_attribution.py`
- Modify: `src/telegram_kol_research/position_attribution_repair.py`
- Test: `tests/test_position_attribution.py`
- Test: `tests/test_position_attribution_repair.py`

**Step 1: Write the failing deterministic assignment test**

Use the Miya-shaped component with legs 244/245 and positions ending 3507/3509.
Call the matcher with `allow_equivalent_permutation=True` and require:

```python
assert result.assignments == {
    244: "1001124099803507",
    245: "1001124099803509",
}
assert result.evidence_by_leg[244]["evidence_type"] == (
    "equivalent_permutation_assignment"
)
```

Reverse every input list and require exactly the same output. With the flag
omitted/false, require the existing unresolved conflict.

**Step 2: Run the test and verify it fails**

Expected: the matcher does not accept the new flag and reports a conflict.

**Step 3: Implement canonical pairing behind an explicit flag**

After normal mutual-unique matching, classify remaining connected components.
For eligible components only, sort legs by integer leg ID and positions by
numeric-aware position ID and pair them. Persist evidence containing:

- `policy_version`;
- `evidence_type=equivalent_permutation_assignment`;
- all component leg and position IDs;
- the compared equivalence signature;
- `mapping_basis=stable_sorted_canonicalization`; and
- a statement that binding ownership was proven but parent-child mapping was
  canonicalized.

Do not enable the flag in ordinary reconcile.

**Step 4: Enable the flag only in repair-plan construction**

Pass `allow_equivalent_permutation=True` from
`build_position_attribution_repair_plan()`. Keep
`reconcile_deepcoin_execution_bindings()` on the default false path. This
ensures a newly deployed service cannot write canonical assignments before the
operator reviews dry-run output.

**Step 5: Add repair safety tests**

Require the repair plan to contain exactly two Miya assignment actions and no
unrelated clears. Add negative tests for:

- another unresolved component;
- changed exchange positions between plan and apply;
- database fingerprint drift;
- repeated apply; and
- an API error.

**Step 6: Run repair tests**

```bash
.venv/bin/pytest -q tests/test_position_attribution.py \
  tests/test_position_attribution_repair.py tests/test_cli_smoke.py
```

Expected: PASS.

**Step 7: Commit**

```bash
git add src/telegram_kol_research/position_attribution.py \
  src/telegram_kol_research/position_attribution_repair.py \
  tests/test_position_attribution.py tests/test_position_attribution_repair.py
git commit -m "feat: repair equivalent position permutations"
```

### Task 6: Authorize reviewed canonical assignments and expose their provenance

**Files:**
- Modify: `src/telegram_kol_research/position_attribution.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `src/telegram_kol_research/templates/_exchange_positions_panel.html`
- Test: `tests/test_deepcoin_execution_actions.py`
- Test: `tests/test_web_app.py`
- Test: `tests/test_web_page_render.py`

**Step 1: Write failing authority and rendering tests**

Persist two verified legs with reviewed
`equivalent_permutation_assignment` evidence and require binding-level close and
TPSL management to pass the existing exact-position authority gate. Persist the
same evidence without the current policy version or component details and
require fail-closed rejection.

Require Web cards to show the Miya group and a label such as “等价腿确定性归属”
instead of claiming Deepcoin direct-ID proof.

**Step 2: Run the focused tests and verify failure**

Run the exact new pytest nodes. Expected: authority helper or rendering does not
yet recognize the evidence type.

**Step 3: Validate the persisted evidence schema in the authority helper**

Extend `has_authoritative_persisted_position()` to trust this evidence only when
the policy version, component IDs, mapping basis, and current leg/position are
consistent. Continue accepting direct order-position identity, explicit
response `posId`, current policy evidence, and explicit manual binds under their
existing rules.

**Step 4: Render provenance without weakening mutation checks**

Map the evidence type to a dedicated label/reason in the positions panel. Do not
derive authority from the display label; keep the existing server-side gate as
the only mutation authority.

**Step 5: Run authority and Web tests**

```bash
.venv/bin/pytest -q tests/test_deepcoin_execution_actions.py \
  tests/test_web_app.py tests/test_web_page_render.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add src/telegram_kol_research/position_attribution.py \
  src/telegram_kol_research/web_app.py \
  src/telegram_kol_research/templates/_exchange_positions_panel.html \
  tests/test_deepcoin_execution_actions.py tests/test_web_app.py \
  tests/test_web_page_render.py
git commit -m "feat: authorize reviewed equivalent positions"
```

### Task 7: Document the invariant and run local verification

**Files:**
- Modify: `docs/migration-handoff.md`
- Modify: `docs/runbook.md`

**Step 1: Document operational behavior**

State that one-price strategies submit one entry leg, normalized duplicate legs
are coalesced, TP/SL is only auxiliary evidence, and reviewed equivalent
permutations use deterministic rather than random mapping. Document that repair
dry-run is mandatory and ordinary reconcile cannot originate this evidence.

**Step 2: Run focused safety suites**

```bash
.venv/bin/pytest -q \
  tests/test_deepcoin_order_builder.py \
  tests/test_recovery_live_submit.py \
  tests/test_position_attribution.py \
  tests/test_position_attribution_repair.py \
  tests/test_execution_bindings.py \
  tests/test_deepcoin_execution_actions.py \
  tests/test_web_app.py
```

Expected: PASS.

**Step 3: Run the full local suite**

```bash
.venv/bin/pytest -q --tb=short
```

Expected: all tests pass. Record any pre-existing environment-only baseline
failure separately; do not weaken tests to hide it.

**Step 4: Request code review and resolve all P0/P1 findings**

Use `@superpowers:requesting-code-review`. Re-run affected focused suites and
`git diff --check` after changes.

**Step 5: Commit documentation/final adjustments**

```bash
git add docs/migration-handoff.md docs/runbook.md
git commit -m "docs: record equivalent entry attribution rules"
```

### Task 8: Push, deploy, dry-run, and repair production

**Files:**
- Verify: `scripts/server_git_update.sh`
- Verify: `scripts/server_git_update.ps1`

**Step 1: Confirm deployment preconditions**

Verify the local branch is `codex/deepcoin-auto-trading-v1`, the worktree is
clean, all intended commits are present, and the production global
`auto_trade_enabled` value is false. Do not re-enable it in this task.

**Step 2: Push reviewed commits**

```bash
git push origin codex/deepcoin-auto-trading-v1
```

Expected: GitHub branch advances to the reviewed local HEAD.

**Step 3: Deploy with the existing helper**

```bash
./scripts/server_git_update.sh
```

Expected: server fast-forwards, reinstalls editable package, and restarts
`telegram-kol.service`. Account for the known graceful-stop timeout and verify a
new active PID rather than assuming the first status line is final.

**Step 4: Discard the pre-close repair plan and fetch current exchange truth**

The operator manually closed the two unattributed positions suspected to belong
to Miya before this code was pushed or deployed. Treat every earlier repair plan
and fingerprint as invalid. Fetch a fresh coherent Deepcoin snapshot containing
current positions, open TPSL orders, pending triggers, and relevant regular and
trigger entry-order history. Confirm ordinary reconcile did not originate new
`equivalent_permutation_assignment` evidence. Do not assume the previously
observed position IDs still exist or still require assignment.

**Step 5: Create a fresh online database backup**

Create a timestamped copy of `data/research.db`, record byte size and SHA-256,
and keep the service/global auto-trade state unchanged.

**Step 6: Audit stale state and run a fresh repair dry-run only**

Before running the planner, audit residual protection/trigger orders and the
affected execution legs, binding, and lifecycle terminal state. A position
absent from the current Deepcoin snapshot must not receive verified ownership
and must not be targeted by a close request. Any stale records or residual
exchange orders require explicit evidence and separate review; do not infer
ownership from TP/SL alone.

```bash
cd /opt/telegram-kol-analyzer
.venv/bin/telegram-kol-research repair-position-attribution \
  --database-path data/research.db
```

Expected result is determined only by the new snapshot. Zero actions is valid
when the manually closed positions no longer exist and no stale database state
requires repair. Review every proposed action and unresolved conflict, and
confirm there are no unrelated changes. Stop without apply if any action lacks
fresh evidence or was inherited from the invalid pre-close plan.

**Step 7: Apply only an unchanged, newly reviewed plan**

Run the CLI with `--apply`. The apply path must rebuild/fingerprint current
exchange and database evidence and refuse any drift. Do not run `--apply` when
the new dry run has zero actions. Any nonzero plan requires separate explicit
review; approval of the obsolete two-assignment plan does not carry forward.

**Step 8: Restart and verify production state**

Restart `telegram-kol.service`, then verify:

- service active and HTTP endpoints healthy;
- global auto trade still false;
- a repeated fresh repair dry-run has zero remaining actions and reports any
  unresolved conflict without mutation;
- manually closed position IDs are not displayed, assigned, or sent a close;
- residual protection/trigger orders and affected leg/binding/lifecycle terminal
  states match the current exchange snapshot;
- any surviving live position card and authority result is supported by its own
  current reviewed evidence; and
- no unrelated binding or leg changed.

Do not report the old two-position Miya outcome as production-verified. Record
the actual post-deployment snapshot and audit result instead.

**Step 9: Run server-focused tests**

Run the builder, submission, attribution, repair, authority, and Web suites on
the server. Report the known production-data homepage empty-state test
separately if the full server suite still encounters it.

**Step 10: Keep automatic trading disabled**

Re-enabling automatic trading requires a separate explicit user confirmation.
