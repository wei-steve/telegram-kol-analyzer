# Protection Order Side Semantics Implementation Plan

**Goal:** Correct order-side versus position-side confusion without weakening any unrelated protection/ownership/readiness gate.

**Architecture:** The owner explicitly authorized choosing separate checks or purpose-aware conversion. Choose separate checks: posSide/pos_side retain their position alias meaning; an optional protective order side must be the opposite buy/sell direction. Do not infer a position identity from side alone. Preserve existing live-position interpretation (including equivalent buy/long and sell/short), which is a different input domain. Reuse one small pure side validator for the two TPSL executors and the protection-candidate builder; do not change the general native matcher or infer ownership.

**Tech Stack:** Python, existing pure matchers, disposable SQLite pytest fixtures. Isolated branch codex/protection-order-side-semantics, base 41c618936f9a598b3641d7be4e6a04d8b98b0f3f; src/tests identical to deployed 9501a5f3 baseline. Existing alert worktree remains untouched.

## Semantics and boundaries

- Protective TPSL: long + sell and short + buy are normal; long + buy and short + sell are rejected. Conflicting posSide/pos_side aliases, unknown supplied order direction, or order side without a provable position direction remain rejected. Missing/blank order side remains optional as before; absence is not permission to skip any other matcher checks.
- Entry/Conditional orders have opening-direction semantics; never apply the protective reversal to them or to live-position rows.
- Preserve path A, numeric/order/instrument/position/type alias checks, the current-size then sz=0 matcher branches, exact fingerprint other predicates, account uniqueness, provenance, ownership, leases, and write gates.
- Same bug found in trigger_backup_stop_executor native alias filter and entry_protection_ledger_repair pending TPSL candidate aliases; inspect their distinct call domains before editing. protection_snapshot and backup_stop_repair use native_tpsl position fields, not a combined position/order alias group; no broad normalizer changes planned.
- Existing test that calls short+sell TPSL an equivalent alias encodes the bug. Preserve live-position short+sell assertion; correct its protective order fixture to short+buy, and separately assert rejection of short+sell. Never preserve a false-positive expectation by weakening the requested semantic rule.
- L3-sensitive protection evidence interpretation; this phase authorizes local implementation/tests/review only. No deployment, production state/data/schema changes, exchange writes, existing intent/convergence recovery, or commit/push. Rollback for a future release is a separate authorization; local candidate can be discarded without touching production.

## Steps

1. RED: new tests/test_protection_order_side_semantics.py reproduces leg 583 full TPSL row, raw position and verified ledger. Expect aliases accepted, primary verification and fingerprint present; baseline fails. Test both executor filters and backup primary readback. Add account-wide candidate proof regression and genuine contradiction negatives before changing each production path.
2. GREEN: introduce pure directional relationship helper in native_tpsl.py; use it at both executor TPSL alias filters, keeping all other groups unchanged. Remove only order side from TPSL position-set comparisons (backup exact match and unowned-TP scoping), after validating the reverse relationship. Leave live-position callers unchanged.
3. Correct pending TPSL candidate side_aliases and guard eligibility against wrong close side; never drop an invalid candidate from the account universe in a way that hides competition. Test parent/child/pos ownership and legacy repair routes remain fail-closed.
4. Focused pytest after each edit; preserve alias-negative tests and prove valid/invalid long/short, sz=0, missing side/position direction, unknown side, duplicate IDs, identity/price/size conflicts, unaffected live-position and Conditional semantics.
5. Read-only historical impact: use mode=ro/query_only=ON, python -B, saved snapshot/order IDs linked to exact legs. Separate current 115 task cohort (74 no posId/40 absent positions/227 live) from historically exposed legs; distinguish demonstrable matching delta from unknown historical gate outcomes. No production matchers that can write or manually trigger recovery.
6. Independent review explicitly audits real-anomaly rejection and all unchanged safety gates. Assemble final production code, run one complete pytest, record exact fingerprint/results. If production code changes afterward, rerun affected tests and final full suite. No integration/deployment. Exactly one Telegram stop notice at handoff.

## Review correction before final testing

The first independent review reproduced two unsafe filter-order effects: an invalid same-ID row could disappear before exact-order counting, and an invalid different-ID legacy candidate could disappear before candidate uniqueness. Both have RED tests. The final design counts all raw identity claims before TPSL filtering; legacy scope predicates remain unchanged and directional contradictions produce a refusal inside the original candidate domain instead of removing rows. The account allocator retains invalid candidates as evidence_complete=False, preserving competition. No write gate is bypassed. The fingerprint function and path-A function remain byte-for-byte AST-equivalent to the baseline.

The repository has two older positive tests encoding short+sell TPSL as an alias: one in test_trigger_take_profit_convergence_executor.py and one in test_execution_bindings.py. Their live-position short+sell coverage must remain; their protective TPSL fixtures use buy, with genuine sell contradiction negatives retained. This is a semantic fixture correction, not dropping the anomaly tests.
