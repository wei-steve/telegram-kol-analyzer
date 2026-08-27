# Phase 8: Authoritative Scope Decision and Implementation Gate

**Status:** design and executable planning complete; implementation blocked on
one owner scope decision.

**Claim base:** `ce2febd11b137bf66fad8db201b366381cbb5817`

**Goal:** determine Phase 8 without inventing a new runtime, trading,
production-data, monitoring, or failure-semantics requirement after Phase 7
accepted `per_chat + 3 + queue`.

**Current risk level:** L0. This phase file and its canonical pointer are local
documentation only. No production code, runtime, database, Telegram, Deepcoin,
or exchange path is read or changed.

## Authoritative Inputs

Read only:

1. `AGENTS.md`;
2. `docs/per-chat-durable-lanes-status.md`;
3. `docs/plans/2026-08-23-per-chat-durable-lanes.md`;
4. `docs/plans/2026-08-25-per-chat-activation-event-loop-optimization.md`.

The authoritative inputs establish all of the following:

- Phase 7 is complete and production remains accepted at
  `per_chat + 3 + queue`.
- The seven-session activation plan defines Phases 1 through 7 only. It names
  no Phase 8 target, files, behavior, or acceptance criteria.
- The total durable-lanes plan says successful cutover acceptance proceeds to
  Task 15 and the terminal canonical state `workstream_status: completed` and
  `current_task: done`.
- The canonical status instead records `workstream_status: in_progress` and
  makes Phase 8 eligible, while intentionally leaving `current_phase_file`
  null. It does not say whether Phase 8 is closeout, monitoring hardening,
  higher concurrency, tooling cleanup, or another goal.

Therefore the existence of Phase 8 is authoritative, but its implementation
target is not uniquely defined. This phase must not silently reinterpret
eligibility as authorization for a behavior change.

## Bounded Design Alternatives

### A. Terminal workstream closeout (recommended)

Reconcile the canonical ledger with the already completed total plan by setting
the workstream to `completed` and `current_task` to `done`, while preserving the
accepted production tuple and all Phase 7 evidence.

- Risk: L0 documentation only.
- Monitoring impact: none; no collector, cadence, threshold, attribution, or
  alert changes.
- Gate impact: none; Phase 7 acceptance remains the terminal production gate
  and is neither rerun nor weakened.
- Rollback: revert only the local documentation commit if the ledger state is
  wrong. No runtime rollback exists or is needed.
- Completion: canonical terminal fields and history agree with the total plan;
  static Markdown/diff checks pass; explicit-path local commit created.
- Production validation eligibility: not applicable. Phase 7 already supplied
  production acceptance and this option changes no production artifact.

### B. Post-cutover monitoring hardening

Define a new, separate monitoring deliverable while keeping the accepted
runtime path untouched. This cannot start until the owner states what must be
monitored, for how long, with which thresholds, and whether the output is
advisory or gating.

- Risk: L1 if purely additive/read-only and dormant; L2 if it changes an
  acceptance/rollback gate, runtime sampling, authority propagation, or fault
  response.
- Monitoring impact: necessarily non-zero and currently unspecified. Sampling
  cadence, collection source, self-perturbation budget, attribution, retention,
  and alert destination must be designed explicitly.
- Gate impact: currently unspecified. A new gate must not rewrite or waive
  Phase 7 evidence, and incomplete monitoring evidence must remain unknown.
- Rollback: a tested disable path is required before enablement; any runtime or
  production action remains a separately authorized later phase.
- Completion: approved monitoring contract, RED-to-GREEN tests for every code
  behavior, focused tests, independent review, and one final complete suite on
  the frozen local candidate. Production verification is not authorized here.

### C. Another owner-specified Phase 8 target

The owner may supply a different concrete target. Any target that changes lane
cap, scheduling, runtime mode, data flow, process authority, state propagation,
deployment, rollback, production data, fault semantics, recognition, strategy,
position, execution, or exchange-write behavior requires a new explicit design
and risk classification before code is touched.

## Fixed Boundaries

- Preserve `message_processing_jobs` as the only durable ordering authority.
- Preserve current recognition, contextual strategy resolution, position,
  management, execution, retry, and exchange-write semantics.
- Preserve production `per_chat + 3 + queue` unless a later exact production
  phase explicitly authorizes a different expected-state transition.
- Do not push, deploy, restart, cut over, roll back, edit production settings or
  data, replay, invoke worker commands, manufacture Telegram traffic, test
  trade, or perform an exchange write in this scope-definition phase.
- Stage explicit paths only; never use `git add -A`.
- A future production-code implementation must use strict RED-to-GREEN TDD,
  focused verification, independent review, and one final complete suite after
  the last production-code edit.

## Executable Plan After the Decision

1. Re-run the clean-tree, current-pointer, ownership, exact local HEAD/upstream/
   remote-tip, latest-status-commit, and Git-lock gates. Stop on mismatch; do
   not repair it.
2. Claim exactly the owner-selected Phase 8 target in the canonical status.
3. Replace this unresolved section with the selected target's exact files,
   behavior, risk, monitoring impact, gate impact, rollback, tests, acceptance,
   and remaining authorization.
4. For option A, make only the L0 canonical closeout change and run static
   checks. RED, GREEN, focused pytest, independent code review, and the complete
   suite are not applicable because no production code or behavior changes.
5. For option B or C, stop again if the approved target still lacks a unique
   runtime/failure contract. Otherwise write the failing tests first, verify
   the expected RED, implement the minimum GREEN, run the directly related
   focused slice, obtain independent review, repair findings through new REDs,
   and run one final complete suite on the final local candidate.
6. Update canonical evidence, release the claim, stage only the named paths,
   inspect cached paths, and commit locally. Do not push or perform production
   verification without separate authorization stated by the final phase file.

## Only Owner Decision Required

Should Phase 8 be **A) terminal workstream closeout**, **B) post-cutover
monitoring hardening**, or **C) another concrete target that you specify**?

Until that single question is answered, Phase 8 implementation is ineligible;
the accepted production state remains `per_chat + 3 + queue` and no runtime or
production rollback is warranted.
