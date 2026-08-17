---
name: risk-adaptive-engineering
description: Classify and route engineering work in this Telegram and Deepcoin auto-trading repository into L0 main-Sol, L1 compact-team, or L2 full-safety workflows. Use before any request to change, build, fix, deploy, restart, migrate, backfill, or modify behavior; also use for diagnosis or review when read-only delegation may help. Hard-route trading, orders, positions, TP/SL, attribution, risk limits, persistent state, concurrency or recovery, private Deepcoin writes, production actions, and uncertain impact to L2.
---

# Risk-Adaptive Engineering

Select the smallest engineering workflow that satisfies the task's real risk while preserving authorization and production safety.

## Route the task

1. Read the repository root `AGENTS.md` completely.
2. Preserve every higher-priority authorization, production, notification, deployment, and specialized Runtime Incident Agent rule.
3. Classify the request's authorization before classifying technical risk:
   - Answer, explain, or report status: inspect and answer only.
   - Diagnose or review: allow read-only investigation; do not implement.
   - Change, build, fix, or deploy: allow only the in-scope implementation and validation normally implied by that request.
4. Read [risk-matrix.md](references/risk-matrix.md).
5. Inspect enough repository evidence to identify real execution paths and impact boundaries.
6. Assign L0, L1, or L2. Choose the higher level when uncertain. Never downgrade an L2 hard trigger.
7. Before editing or delegating, confirm the complete router is installed:
   - Root `AGENTS.md` contains `Risk-Adaptive Engineering Workflow`.
   - `.codex/config.toml` enables agents and bounds spawned concurrency.
   - `.codex/agents/` contains `scout`, `builder`, `reviewer`, `tester`, and `production-verifier` definitions.
8. If any installation control is missing or invalid, return `SETUP_INCOMPLETE`, provide the classification only, and stop before edits, delegation, or production access.
9. Keep this routing record in task context and include it in the final handoff:

```text
RISK_LEVEL:
TRIGGERS:
AUTHORIZED_ACTION:
TEAM:
COMPLETION_GATE:
```

Do not turn the routing record into an intermediate project notification.

## Execute L0

Use only the main agent.

1. Make the isolated change.
2. Run proportionate local validation.
3. Report the risk record and evidence.

Do not spawn workers merely to satisfy a process preference.

## Execute L1

1. Define explicit scope, exclusions, acceptance criteria, expected files, and validation.
2. Use `scout` only when the code path or impact boundary needs investigation.
3. Delegate implementation to one `builder` after the plan is stable.
4. Delegate independent review to `reviewer` after implementation.
5. Return concrete failures to the same builder, then rerun review.
6. Let the main agent accept the result only when every criterion passes.

## Execute L2

1. Define explicit scope, exclusions, acceptance criteria, safety boundaries, rollback, and server evidence requirements.
2. Delegate read-only investigation to `scout`.
3. Let the main agent approve one implementation plan. Do not delegate unresolved architecture decisions.
4. Delegate local implementation to one `builder`.
5. After implementation, run `reviewer` and `tester` independently. Parallelize them only when their work is independent.
6. Return concrete failures to the same builder, then rerun both validation lanes.
7. Allow no more than two normal builder repair rounds. After two failed rounds, stop implementation and let the main agent reassess the specification or architecture.
8. Perform release mutations only through the authorized main project workflow.
9. Use `production_verifier` only after an authorized release and only for read-only server evidence.
10. Require reviewer PASS, tester PASS, every acceptance criterion, and any required production evidence before completion.

## Delegate with contracts

Read [handoff-contracts.md](references/handoff-contracts.md) before spawning any role.

Include the common assignment fields in every delegation and require the role-specific return fields. Give each worker the minimum context needed to do its bounded job. Do not pass secrets or unrelated production data.

The main Sol agent is the sole delegation owner. Tell every worker not to spawn agents or delegate work.

Before dispatch, record a clean-room baseline with `git status --short` and the relevant diff. Apply these runtime gates:

- Establish effective sandbox evidence from the current session permission context: in the app, the composer permission mode for the parent turn; in the CLI, the active `--sandbox`/`/permissions` state. Local subagents inherit that live parent policy. Never infer effective isolation from a TOML default.
- If the active parent is read-only, it may dispatch `scout` or `reviewer` through the collaboration runtime. If the parent is write-enabled, run the role as a separate bundled-Codex process explicitly started with `--sandbox read-only`, approval policy `never`, the repository working directory, the configured model/effort, and the full role contract. Capture its final output as evidence. If a separate read-only session cannot be established, return `READ_ONLY_RUNTIME_REQUIRED` and stop that workflow.
- Run `production_verifier` only in a separate verified read-only session after an authorized release. If the required server evidence cannot be gathered without a write, stop and report the missing evidence.
- `tester` may use workspace-write only for test artifacts. Compare the post-run status and diff with the baseline; any application-source change makes the tester result FAIL and must not be accepted.
- Compare the post-run status and diff after every read-only role. Any mutation makes the role result FAIL, invalidates its evidence, and requires the main agent to preserve or restore user-owned work safely before proceeding.

Prefer a runtime-registered named custom role when the spawn interface actually exposes it. If the current runtime does not expose named-role selection, do not pretend that `task_name` loads the TOML. Use the supported fallback: read the matching role TOML, spawn with its explicit model and effort, use `fork_turns = "none"`, and include its full developer instructions plus the handoff contract in the assignment. Record `EXPLICIT_ROLE_FALLBACK` in `TEAM`.

Isolated read-only processes are still delegated lanes owned by main Sol. Their prompt must prohibit further delegation and notifications. Record `ISOLATED_READ_ONLY_SESSION` in `TEAM`; do not count an isolated process as a builder repair round.

## Preserve ownership and independence

- Assign one implementation owner.
- Never let multiple agents modify overlapping files concurrently.
- Prefer parallel read-only investigation and validation.
- Keep reviewer and tester independent from the builder.
- Configure read-only roles conservatively, but treat custom-agent sandbox settings as defaults rather than proof of isolation.
- Preserve the parent session's real permission boundary and enforce the dispatch gates above.
- Do not let a reviewer repair its own finding.
- Do not let the main Sol agent rewrite an imperfect implementation merely to avoid a builder repair round.

## Honor other skills

Use other applicable skills when their triggers match. This router selects risk and team topology; it does not replace brainstorming, planning, systematic debugging, test-driven development, code review, or domain-specific instructions.

If another skill requires an approval or pause, honor it. Do not reinterpret a larger team as permission to bypass a gate.

## Stop safely

Stop and return control when:

- The task requires authority the user did not grant.
- A safe production window cannot be proven.
- The approved plan is inconsistent or unsafe.
- Validation exposes an architecture or specification failure.
- Required server verification depends on unavailable identity, allowlisting, or secrets.
- Two normal repair rounds fail.

Report the exact remaining work and preserve the current production path.
