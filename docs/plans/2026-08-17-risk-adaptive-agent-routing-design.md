# Risk-Adaptive Codex Team Design

**Goal:** Let the main Sol agent automatically choose a single-agent, compact-team, or full safety-team workflow for each project task while preserving the repository's existing trading, deployment, notification, and production-safety rules.

## Selected approach

Use a project-scoped `risk-adaptive-engineering` skill as the routing procedure, a short mandatory trigger in the root `AGENTS.md`, and project-scoped custom agents for bounded roles. The main Sol agent classifies each authorized task before implementation and selects the smallest workflow that satisfies the risk rules.

The design deliberately separates three concerns:

- `AGENTS.md` makes risk classification mandatory for change, build, fix, and deploy requests.
- `.agents/skills/risk-adaptive-engineering/` contains the reusable classification and orchestration procedure.
- `.codex/agents/` defines the models, permissions, responsibilities, and output contracts of delegated roles.

A skill alone is not the enforcement boundary because implicit skill matching is model-selected. The root instruction is the durable entry point; the skill keeps the detailed policy modular and maintainable.

The skill also has a construction-state preflight. Until the root trigger, bounded `[agents]` configuration, and all five custom-agent definitions exist, direct invocation may classify a request but must return `SETUP_INCOMPLETE` before editing or delegating. This keeps both implicit and explicit invocation dormant as an execution path during partial installation or rollback.

## Scope

This phase configures local Codex workflow behavior for this repository. It does not modify Telegram processing, strategy recognition, Deepcoin execution, database state, production services, credentials, or trading settings.

It also does not make deployment automatic. Agent routing may be automatic, but external writes and production actions remain governed by the user's request and the existing project workflow.

## Automatic invocation

For every request to change, build, fix, deploy, or modify behavior, the main agent must invoke `risk-adaptive-engineering` before editing files or delegating implementation.

Read-only requests follow the existing authorization boundary:

- Answer, explain, or report status: inspect and answer without starting an implementation team.
- Diagnose or review: the router may choose parallel read-only investigation, but must not authorize implementation.
- Change, build, fix, or deploy: classify and execute the applicable L0, L1, or L2 workflow.

If another applicable skill imposes a stronger workflow, the router coordinates with it rather than replacing it. In particular, brainstorming, planning, debugging, test-driven development, and code-review requirements remain authoritative when triggered.

## Risk levels

### L0 — Main Sol only

Use L0 when all of the following are true:

- The change is isolated, well understood, and locally verifiable.
- It does not affect trading decisions, orders, positions, protection, attribution, persistent state, production runtime, or external writes.
- It does not change a shared interface or cross a meaningful module boundary.
- Failure has low and readily reversible impact.

Typical examples are documentation corrections, narrowly scoped test maintenance, and a small non-production presentation change.

Flow:

```text
Sol -> implement -> local validation -> final evidence
```

### L1 — Compact engineering team

Use L1 when any ordinary engineering-risk condition applies and no L2 hard trigger applies. Examples include multi-file changes, internal interface changes, ordinary feature work, uncertain defects, or meaningful regression risk.

Flow:

```text
Sol defines acceptance criteria
  -> optional Luna scout
  -> Terra builder
  -> independent Terra reviewer
  -> builder repair when needed
  -> Sol accepts evidence
```

The reviewer never repairs its own findings. Validation failures return to the same builder.

### L2 — Full safety team

Any of these is a hard L2 trigger:

- Automated order placement, cancellation, or amendment.
- Position management, partial close, breakeven, take-profit, or stop-loss behavior.
- Strategy recognition, message context, targeting, attribution, or group isolation.
- Risk limits, contract specifications, quantity, price, or leverage calculations.
- Database migration, historical backfill, state repair, or ownership recovery.
- Concurrency, retry, idempotency, recovery, or compensation behavior.
- Deepcoin private write APIs or any production write path.
- Deployment, package reinstall, service restart, or a change that may affect current positions.
- Unknown root cause or an impact boundary that cannot be established confidently.
- An explicit user request for the highest safety workflow.

Flow:

```text
Sol defines scope, acceptance criteria, and safety boundary
  -> Luna scout maps code, tests, runtime dependencies, and risks
  -> Sol approves one implementation plan
  -> Terra builder implements locally
  -> Terra reviewer and Luna tester validate independently
  -> the same builder repairs concrete failures, at most two normal rounds
  -> Sol reassesses only specification or architecture failures
  -> main workflow performs authorized release actions
  -> Terra production verifier gathers read-only server evidence
```

When classification is uncertain, choose the higher level. No agent may downgrade an L2 hard trigger.

## Agent roles

| Role | Model | Effort | Default permission | Responsibility |
| --- | --- | --- | --- | --- |
| Main architect | GPT-5.6 Sol | Current project setting | Existing main-thread permission | Classify risk, define acceptance criteria, own architecture, arbitrate, approve |
| Scout | GPT-5.6 Luna | Medium | Read-only default; verified effective read-only required | Map files, symbols, execution paths, tests, dependencies, and risks |
| Builder | GPT-5.6 Terra | High | Workspace write | Implement only the approved plan and run relevant local checks |
| Reviewer | GPT-5.6 Terra | High | Read-only default; verified effective read-only required | Independently find correctness defects, regressions, scope drift, and missing tests |
| Tester | GPT-5.6 Luna | Medium | Workspace write plus diff guard | Run tests and reproduce behavior; application-source mutations invalidate the result |
| Production verifier | GPT-5.6 Terra | High | Separate verified effective read-only session | Verify deployed commit, service state, health, and required server evidence |

Custom-agent sandbox values are defaults: parent or live session overrides can widen the effective runtime. Before dispatching a read-only role, the main agent must verify that the effective child runtime remains read-only; otherwise it stops with `READ_ONLY_RUNTIME_REQUIRED`. The production verifier runs only in a separate verified read-only session, and its instructions prohibit deploying, restarting, repairing, or compensating. Release mutations remain with the authorized main workflow.

Effective sandbox evidence comes from the parent turn's live permission context, not from the custom-agent TOML: the selected composer mode in the app or active `--sandbox`/`/permissions` state in the CLI. Current local Codex documentation states that local subagents inherit that live policy and that CLI live overrides are reapplied when a child starts.

This creates a deliberate two-session pattern for write-enabled tasks. Builder and tester work can remain in the active write-enabled task. Scout, reviewer, and production-verifier lanes run either as children of an already read-only parent or as separate bundled-Codex processes started with an explicit read-only sandbox and no approval escalation. The main Sol process supplies the model, effort, role instructions, contract, and repository path, then captures the final result. If that isolated boundary cannot be established, the workflow stops with `READ_ONLY_RUNTIME_REQUIRED` instead of silently reviewing inside a write-enabled child.

The dispatcher records the worktree status and relevant diff before and after delegated work. Any mutation by a read-only role invalidates its evidence. Tester application-source changes likewise make the test result fail. The main Sol agent is the sole delegation owner; custom workers never spawn agents or delegate work.

Only the user-facing main orchestrator sends the repository's single stop notification. Every delegated or isolated lane is intermediate work and is explicitly prohibited from notifying.

The checked bundled runtime accepts the standalone custom-agent files structurally but does not currently expose a machine-readable named-role selector in its `spawn_agent` tool. A `task_name` equal to `scout` produced a generic Sol child, so it is not accepted as evidence that `scout.toml` loaded. Until the runtime exposes named-role selection, the router uses an explicit fallback: it reads the role TOML, passes the configured model and effort to `spawn_agent`, uses no inherited turns, and includes the role instructions and return contract in the assignment. This preserves the intended team behavior without making a false discovery claim.

Validation against bundled Codex `0.148.0-alpha.9` confirmed the fallback: a disposable child started as GPT-5.6 Luna with medium effort and a read-only `turn_context`; its harmless file-creation attempt was denied and the target remained absent. The bundled app-server `config/read` response also attributed `agents.enabled = true` and the concurrency cap of `3` to this project's `.codex` layer.

## Concurrency and ownership

Enable the current subagent workflow with a three-thread spawned-agent cap. The documented `agents.max_concurrent_threads_per_session` value excludes the primary thread. The main Sol orchestrator keeps direct concurrency within that cap, and the no-nested-delegation rule prevents workers from multiplying the topology.

Use parallelism for independent read-only work and validation. Do not let multiple agents modify overlapping files concurrently. If future work truly requires parallel implementation, use isolated worktrees and explicit non-overlapping file ownership; that is outside the first version of this skill.

## Handoff contracts

Every delegation includes:

- Authorized objective and risk level.
- Exact scope and exclusions.
- Acceptance criteria.
- Expected files or read-only investigation boundary.
- Commands or evidence required.
- A structured return contract.

Builder returns changed files, implementation summary, commands run, results, deviations, and unresolved risks. Reviewer returns a pass/fail verdict and actionable findings with file/line evidence. Tester returns commands, results, reproduction evidence, regressions, and failures. The production verifier returns the deployed revision, service evidence, safety-gate evidence, and a pass/fail verdict.

## Repair and escalation

- Concrete implementation or test failures return to the same builder.
- The main Sol agent does not rewrite an imperfect implementation merely to save a repair round.
- Run reviewer and tester again after each repair.
- Allow at most two normal repair rounds.
- After two failed rounds, stop implementation and let Sol reassess the specification or architecture.
- If the plan itself is unsafe or inconsistent, the builder stops rather than inventing a new architecture.

## Project files

Create:

```text
.agents/skills/risk-adaptive-engineering/
├── SKILL.md
├── agents/openai.yaml
└── references/
    ├── handoff-contracts.md
    └── risk-matrix.md

.codex/
├── config.toml
└── agents/
    ├── builder.toml
    ├── production-verifier.toml
    ├── reviewer.toml
    ├── scout.toml
    └── tester.toml
```

Modify the root `AGENTS.md` only to add the mandatory trigger, precedence rules, and compact completion gate. Keep detailed matrices and role contracts out of the root file to avoid bloating every task context.

No scripts or assets are needed in the first version. Natural-language task risk requires model judgment, while hard triggers and examples provide the necessary guardrails. A keyword-only classifier would be brittle and create false confidence.

## Validation

Validate in four layers:

1. Run the Skill Creator structural validator on the project skill.
2. Run the Codex target validator, then use the bundled app-server `config/read` API from the project directory to confirm the effective `[agents]` values and their project-layer origins.
3. Inspect model-visible prompt input to confirm the root `AGENTS.md` rule and skill metadata are discoverable.
4. In a disposable copy that omits this repository's notification hook, test named-role selection if the spawn API exposes it. Otherwise prove the explicit-role fallback by asserting the spawned child's model, reasoning effort, and inherited sandbox from its runtime `turn_context`; require a harmless write to be denied and the target file to remain absent.
5. Forward-test classification without implementation using representative prompts:
   - Documentation typo -> L0.
   - Small isolated non-trading UI correction -> L0 or L1 with explicit reasoning.
   - Ordinary multi-file feature -> L1.
   - Order sizing change -> L2.
   - Production restart request -> L2.
   - Ambiguous position-management defect -> L2.

Forward tests must ask only for classification and route selection, must not edit files, contact production, send messages, or start trading actions.

## Rollout

Introduce the configuration without deploying or restarting the production service. Keep implicit skill invocation disabled and keep the mandatory root trigger absent while the skill, bounded custom agents, project config, proposed trigger text, runtime config load, supported dispatch smoke test, and routing forward tests are being validated. Add the root trigger and enable implicit invocation together in a final isolated change after every gate passes.

The main agent must report the selected risk level and workflow in its final handoff so routing decisions remain auditable without violating the project's no-intermediate-notification rule.

## Rollback

Rollback is local and immediate:

1. Remove the risk-routing block from `AGENTS.md`.
2. Remove `.agents/skills/risk-adaptive-engineering/`.
3. Remove the project `.codex/agents/` files and `[agents]` project settings if they were created solely for this workflow.
4. Re-run strict config loading to confirm the repository falls back to the global Codex configuration.

No production rollback is required because this configuration does not alter application or server state.

## Acceptance criteria

- Every mutating project task is classified before file edits or implementation delegation.
- L2 hard triggers cannot be downgraded by the main agent.
- L0 tasks do not spawn unnecessary workers.
- L1 and L2 implementation is independently reviewed.
- L2 testing is independent from the builder.
- Failed validation returns to the same builder for no more than two normal rounds.
- Production verification is dispatched only under a verified effective read-only runtime and cannot expand deployment authority through its instructions or workflow contract.
- Existing project workflow, Runtime Incident Agent, Deepcoin documentation, and single-stop-notification rules remain intact.
- The custom skill passes structural validation.
- Project Codex configuration and custom agents pass the target validator; bundled runtime `config/read` confirms the project layer, and a disposable dispatch smoke test confirms either real named-role binding or the explicit-role fallback's model, effort, and inherited sandbox. Any unrelated user-level strict-config failure is recorded without silently modifying global configuration.
- Representative routing forward tests select the expected risk levels.
