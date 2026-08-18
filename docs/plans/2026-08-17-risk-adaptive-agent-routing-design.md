# Risk-Adaptive Codex Team Design

**Goal:** Let the main Sol agent automatically choose a single-agent, compact-team, or full safety-team workflow for each project task while preserving the repository's existing trading, deployment, notification, and production-safety rules.

## Selected approach

Use a project-scoped `risk-adaptive-engineering` skill as the routing procedure, a short mandatory trigger in the root `AGENTS.md`, and project-scoped custom agents for bounded roles. The main Sol agent classifies each authorized task before implementation and selects the smallest workflow that satisfies the risk rules.

The design deliberately separates three concerns:

- `AGENTS.md` makes risk classification mandatory for change, build, fix, and deploy requests.
- `.agents/skills/risk-adaptive-engineering/` contains the reusable classification and orchestration procedure.
- `.codex/agents/` defines the models, permissions, responsibilities, and output contracts of delegated roles.

A skill alone is not the enforcement boundary because implicit skill matching is model-selected. The root instruction is the durable entry point; the skill keeps the detailed policy modular and maintainable.

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
| Scout | GPT-5.6 Luna | Medium | Read-only | Map files, symbols, execution paths, tests, dependencies, and risks |
| Builder | GPT-5.6 Terra | High | Workspace write | Implement only the approved plan and run relevant local checks |
| Reviewer | GPT-5.6 Terra | High | Read-only | Independently find correctness defects, regressions, scope drift, and missing tests |
| Tester | GPT-5.6 Luna | Medium | Workspace write | Run tests and reproduce behavior; never modify application source |
| Production verifier | GPT-5.6 Terra | High | Read-only | Verify deployed commit, service state, health, and required server evidence |

The production verifier cannot deploy, restart, repair, or compensate. Release mutations remain with the authorized main workflow so a read-only verifier cannot silently expand production authority.

## Concurrency and ownership

Enable the current subagent workflow with a three-thread spawned-agent cap. The documented `agents.max_concurrent_threads_per_session` value excludes the primary thread, so this permits the main Sol thread plus at most three concurrent workers, matching the current desktop runtime and keeping coordination bounded.

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
2. Load the project config with the bundled Codex CLI in strict-config mode and confirm the `[agents]` settings and custom-agent files are recognized.
3. Inspect model-visible prompt input to confirm the root `AGENTS.md` rule and skill metadata are discoverable.
4. Forward-test classification without implementation using representative prompts:
   - Documentation typo -> L0.
   - Small isolated non-trading UI correction -> L0 or L1 with explicit reasoning.
   - Ordinary multi-file feature -> L1.
   - Order sizing change -> L2.
   - Production restart request -> L2.
   - Ambiguous position-management defect -> L2.

Forward tests must ask only for classification and route selection, must not edit files, contact production, send messages, or start trading actions.

## Rollout

Introduce the configuration without deploying or restarting the production service. First validate structural discovery and dry-run classifications. Then use it on one L0 documentation task and one synthetic L2 classification-only task before trusting it for real implementation.

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
- Production verification is read-only and cannot expand deployment authority.
- Existing project workflow, Runtime Incident Agent, Deepcoin documentation, and single-stop-notification rules remain intact.
- The custom skill passes structural validation.
- Project Codex configuration passes strict loading on the installed desktop runtime.
- Representative routing forward tests select the expected risk levels.
