# Risk-Adaptive Codex Team Implementation Plan

> **Required skill:** Use `executing-plans` to implement this plan task-by-task.

**Goal:** Configure this repository so the main Sol agent automatically classifies authorized engineering work as L0, L1, or L2 and selects a proportionate single-agent or subagent workflow.

**Architecture:** A mandatory root `AGENTS.md` trigger invokes a project-scoped `risk-adaptive-engineering` skill before mutating work. The skill owns risk classification and orchestration, while project-scoped custom-agent TOML files define narrow Luna and Terra roles. The existing project workflow remains authoritative for authorization, Git, deployment, server verification, and the single stop notification.

**Tech Stack:** Codex Desktop/CLI 0.148+, `AGENTS.md`, Codex project skills, YAML, TOML, GPT-5.6 Sol/Terra/Luna, Git.

---

## Before starting

- Work in a clean dedicated worktree based on `codex/deepcoin-auto-trading-v1` if possible. If the current worktree contains unrelated changes, do not stage, modify, or delete them.
- This plan changes only Codex workflow configuration and documentation. Do not deploy, reinstall the package, restart `telegram-kol.service`, contact Deepcoin private write APIs, or alter production state.
- Use `/Applications/ChatGPT.app/Contents/Resources/codex` for validation because the `codex` wrapper currently points to a missing package binary.
- Keep the existing root `AGENTS.md` rules intact. Additive changes only.

### Task 1: Initialize the project skill

**Files:**
- Create: `.agents/skills/risk-adaptive-engineering/SKILL.md`
- Create: `.agents/skills/risk-adaptive-engineering/agents/openai.yaml`
- Create directory: `.agents/skills/risk-adaptive-engineering/references/`

**Step 1: Prove the target does not already exist**

Run:

```bash
test ! -e .agents/skills/risk-adaptive-engineering
```

Expected: exit code 0. If it already exists, stop and inspect it instead of overwriting it.

**Step 2: Run the Skill Creator initializer**

Run:

```bash
python3 /Users/steven/.codex/skills/.system/skill-creator/scripts/init_skill.py \
  risk-adaptive-engineering \
  --path .agents/skills \
  --resources references \
  --interface 'display_name=Risk-Adaptive Engineering' \
  --interface 'short_description=Route project work through risk-matched agent teams' \
  --interface 'default_prompt=Use $risk-adaptive-engineering to classify this project task and select the safest proportionate workflow.'
```

Expected: the skill folder, `SKILL.md`, `agents/openai.yaml`, and `references/` are created.

**Step 3: Keep implicit invocation dormant during construction**

Ensure `.agents/skills/risk-adaptive-engineering/agents/openai.yaml` contains:

```yaml
interface:
  display_name: "Risk-Adaptive Engineering"
  short_description: "Route project work through risk-matched agent teams"
  default_prompt: "Use $risk-adaptive-engineering to classify this project task and select the safest proportionate workflow."

policy:
  allow_implicit_invocation: false
```

Do not add icons, colors, or tool dependencies. Keep implicit invocation disabled until the bounded custom agents, project config, root trigger, runtime checks, and forward tests all pass in Task 7.

**Step 4: Inspect the generated skeleton**

Run:

```bash
find .agents/skills/risk-adaptive-engineering -maxdepth 3 -type f -print -o -type d -print
```

Expected: only the skill, its `agents` metadata, and its empty `references` directory are present.

**Step 5: Commit the skeleton**

```bash
git add .agents/skills/risk-adaptive-engineering
git commit -m "chore: initialize risk-adaptive engineering skill"
```

### Task 2: Write the risk matrix and handoff contracts

**Files:**
- Create: `.agents/skills/risk-adaptive-engineering/references/risk-matrix.md`
- Create: `.agents/skills/risk-adaptive-engineering/references/handoff-contracts.md`

**Step 1: Write the risk matrix**

Create `risk-matrix.md` with these sections:

```markdown
# Risk Matrix

## Authorization comes first

- Answer, explain, and status requests remain read-only.
- Diagnose and review requests may use read-only delegation but do not authorize fixes.
- Change, build, fix, and deploy requests may enter L0, L1, or L2.

## L0 — Main Sol only

Use only when every condition is true: isolated, well understood, locally verifiable,
no shared interface change, no persistent or production impact, and readily reversible.

## L1 — Compact team

Use for ordinary multi-file work, internal interface changes, ordinary features,
uncertain non-trading defects, or meaningful regression risk when no L2 trigger applies.

## L2 — Full safety team

Any trading decision, order, cancellation, position, protection, TP/SL, partial-close,
breakeven, recognition, context, targeting, attribution, group-isolation, risk-limit,
contract-specification, quantity, price, leverage, persistent-state, migration, backfill,
state-repair, concurrency, retry, idempotency, recovery, compensation, private Deepcoin
write, production write path, deployment, reinstall, restart, current-position,
unknown-root-cause, unclear-impact-boundary, or explicit highest-safety request forces L2.

## Tie breakers

- Any L2 trigger wins over L0 or L1 characteristics.
- Uncertainty raises the level; it never lowers it.
- The main agent may increase risk based on repository evidence but may not downgrade a hard trigger.
- Routing does not expand the user's authorization or production permissions.

## Representative classifications

| Request | Expected route |
| --- | --- |
| Correct a documentation typo | L0 |
| Small isolated non-trading UI correction | L0 or L1, with evidence |
| Add an ordinary multi-file reporting feature | L1 |
| Change order sizing or leverage calculation | L2 |
| Repair strategy attribution | L2 |
| Restart the production service | L2 |
| Diagnose an ambiguous current-position defect | L2 read-only investigation |
```

**Step 2: Write the handoff contracts**

Create `handoff-contracts.md` containing exact required return fields:

```markdown
# Handoff Contracts

## Common assignment

Every assignment states OBJECTIVE, RISK_LEVEL, AUTHORIZED_SCOPE, EXCLUSIONS,
ACCEPTANCE_CRITERIA, REQUIRED_EVIDENCE, and STOP_CONDITIONS.

## Scout return

STATUS, RELEVANT_FILES, EXECUTION_PATH, DEPENDENCIES, EXISTING_TESTS, RISKS, EVIDENCE.

## Builder return

STATUS, FILES_CHANGED, IMPLEMENTATION, TESTS_RUN, TEST_RESULTS,
DEVIATIONS_FROM_PLAN, UNRESOLVED_RISKS.

## Reviewer return

VERDICT: PASS | FAIL, CRITICAL_FINDINGS, MAJOR_FINDINGS, MINOR_FINDINGS,
MISSING_TESTS, ACCEPTANCE_CRITERIA, RECOMMENDED_ACTION.

Findings cite concrete file and line evidence. The reviewer never edits files.

## Tester return

VERDICT: PASS | FAIL, COMMANDS_RUN, TEST_RESULTS, REPRODUCTION_RESULT,
REGRESSIONS, FAILURES.

The tester may create ordinary test artifacts but never modifies application source.

## Production verifier return

VERDICT: PASS | FAIL, DEPLOYED_REVISION, SERVICE_STATE, HEALTH_EVIDENCE,
SAFETY_GATE_EVIDENCE, REMAINING_RISKS.

The production verifier is read-only and never deploys, restarts, repairs, or compensates.
```

**Step 3: Check the hard triggers and contracts are present**

Run:

```bash
rg -n 'L0|L1|L2|order|position|TP/SL|attribution|Deepcoin|deployment|restart|uncertainty' \
  .agents/skills/risk-adaptive-engineering/references/risk-matrix.md
rg -n 'OBJECTIVE|VERDICT|FILES_CHANGED|COMMANDS_RUN|DEPLOYED_REVISION|read-only' \
  .agents/skills/risk-adaptive-engineering/references/handoff-contracts.md
```

Expected: every required category is matched.

**Step 4: Commit the references**

```bash
git add .agents/skills/risk-adaptive-engineering/references
git commit -m "docs: define agent routing risk and handoff contracts"
```

### Task 3: Implement the routing skill

**Files:**
- Modify: `.agents/skills/risk-adaptive-engineering/SKILL.md`

**Step 1: Replace the generated frontmatter**

Use exactly two frontmatter fields:

```yaml
---
name: risk-adaptive-engineering
description: Classify and route engineering work in this Telegram and Deepcoin auto-trading repository into L0 main-Sol, L1 compact-team, or L2 full-safety workflows. Use before any request to change, build, fix, deploy, restart, migrate, backfill, or modify behavior; also use for diagnosis or review when read-only delegation may help. Hard-route trading, orders, positions, TP/SL, attribution, risk limits, persistent state, concurrency or recovery, private Deepcoin writes, production actions, and uncertain impact to L2.
---
```

**Step 2: Write the concise skill body**

The body must implement this procedure:

1. Read the root `AGENTS.md` and preserve all higher-priority authorization, production, notification, and specialized Runtime Incident Agent rules.
2. Determine whether the request authorizes answer, diagnose/review, or change/build/fix/deploy behavior.
3. Read `references/risk-matrix.md` and classify L0, L1, or L2 from the request plus repository evidence.
4. Before editing or delegating, require the root workflow trigger, project `[agents]` config, and all five custom-agent files. If any control is missing or invalid, return `SETUP_INCOMPLETE`, provide classification only, and stop.
5. Keep a routing record with `RISK_LEVEL`, `TRIGGERS`, `AUTHORIZED_ACTION`, `TEAM`, and `COMPLETION_GATE`; report it in the final handoff rather than sending an intermediate notification.
6. Execute the smallest allowed route:
   - L0: main agent only.
   - L1: optional scout, one builder, one independent reviewer.
   - L2: scout, main-agent plan approval, one builder, independent reviewer and tester, then read-only production verifier only after an authorized release.
7. Use the contracts in `references/handoff-contracts.md` for every delegation.
8. Never allow overlapping concurrent writes. Parallelize independent reading and validation only.
9. Return validation failures to the same builder and rerun validation. Stop after two normal repair rounds for main-agent architectural reassessment.
10. Never let routing expand authority, bypass a required skill, or make a production action safe merely because more agents are involved.

Keep `SKILL.md` under 180 lines and avoid copying the detailed risk matrix or handoff schemas into it.

**Step 3: Validate the skill structure**

Run the validator in an isolated `uv` environment that provides PyYAML:

```bash
uv run --no-project --with pyyaml python \
  /Users/steven/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/risk-adaptive-engineering
```

Expected: `Skill is valid!`

**Step 4: Check the UI metadata still matches**

Run:

```bash
sed -n '1,120p' .agents/skills/risk-adaptive-engineering/agents/openai.yaml
```

Expected: the display name, description, default prompt, and implicit-invocation policy still match the implemented skill.

**Step 5: Commit the skill**

```bash
git add .agents/skills/risk-adaptive-engineering/SKILL.md \
  .agents/skills/risk-adaptive-engineering/agents/openai.yaml
git commit -m "feat: add risk-adaptive engineering router"
```

### Task 4: Configure bounded custom agents

**Files:**
- Create: `.codex/agents/scout.toml`
- Create: `.codex/agents/builder.toml`
- Create: `.codex/agents/reviewer.toml`
- Create: `.codex/agents/tester.toml`
- Create: `.codex/agents/production-verifier.toml`

**Step 1: Create the scout**

```toml
name = "scout"
description = "Read-only investigator for mapping code, runtime dependencies, tests, and risk before implementation."
model = "gpt-5.6-luna"
model_reasoning_effort = "medium"
sandbox_mode = "read-only"
developer_instructions = """
Investigate only the assigned scope. Trace real execution paths, cite files and symbols,
find existing tests and similar implementations, and identify concrete risks.
Do not edit files, redesign architecture, expand scope, or propose unsupported fixes.
Do not spawn subagents, delegate work, or send notifications; the main Sol agent owns them.
Stop with READ_ONLY_RUNTIME_REQUIRED unless the dispatcher confirms the effective runtime is read-only.
Return the Scout fields from the risk-adaptive handoff contract.
"""
```

**Step 2: Create the builder**

```toml
name = "builder"
description = "Implementation owner used only after scope, plan, acceptance criteria, and file ownership are defined."
model = "gpt-5.6-terra"
model_reasoning_effort = "high"
sandbox_mode = "workspace-write"
developer_instructions = """
Implement only the approved plan. Make the smallest defensible change, preserve unrelated
files and interfaces, and run relevant local tests. Do not redesign, expand scope, deploy,
restart services, or use production write paths. Stop if the plan is unsafe or inconsistent.
Do not spawn subagents, delegate work, or send notifications; the main Sol agent owns them.
Return the Builder fields from the risk-adaptive handoff contract.
"""
```

**Step 3: Create the reviewer**

```toml
name = "reviewer"
description = "Independent read-only reviewer focused on correctness, regressions, scope drift, and missing tests."
model = "gpt-5.6-terra"
model_reasoning_effort = "high"
sandbox_mode = "read-only"
developer_instructions = """
Review the implementation against the approved plan and every acceptance criterion.
Prioritize correctness, trading safety, behavior regressions, edge cases, interface stability,
scope drift, and missing tests. Cite concrete file and line evidence. Do not edit or repair.
Do not spawn subagents, delegate work, or send notifications; the main Sol agent owns them.
Stop with READ_ONLY_RUNTIME_REQUIRED unless the dispatcher confirms the effective runtime is read-only.
Return the Reviewer fields from the risk-adaptive handoff contract.
"""
```

**Step 4: Create the tester**

```toml
name = "tester"
description = "Validation agent for reproducing behavior and running relevant local tests without editing application source."
model = "gpt-5.6-luna"
model_reasoning_effort = "medium"
sandbox_mode = "workspace-write"
developer_instructions = """
Validate only the assigned behavior. Run relevant unit, integration, and regression checks,
and reproduce the original defect when possible. Do not modify application source, deploy,
restart services, or contact production write paths. Treat infrastructure failures separately.
Do not spawn subagents, delegate work, or send notifications; the main Sol agent owns them.
Return FAIL if the dispatcher detects any application-source change against its baseline.
Return the Tester fields from the risk-adaptive handoff contract.
"""
```

**Step 5: Create the production verifier**

```toml
name = "production_verifier"
description = "Read-only production verifier for deployed revision, service health, and safety-gate evidence after an authorized release."
model = "gpt-5.6-terra"
model_reasoning_effort = "high"
sandbox_mode = "read-only"
developer_instructions = """
Gather only the authorized read-only production evidence. Verify deployed revision, service
state, health, and required safety gates. Never deploy, pull, install, restart, repair,
compensate, place orders, or change server state. Stop if verification would require a write.
Do not spawn subagents, delegate work, or send notifications; the main Sol agent owns them.
Stop with READ_ONLY_RUNTIME_REQUIRED unless the dispatcher confirms a separate effective read-only session.
Return the Production verifier fields from the risk-adaptive handoff contract.
"""
```

**Step 6: Parse every custom-agent TOML file**

Run:

```bash
python3 - <<'PY'
import pathlib
import tomllib

for path in sorted(pathlib.Path('.codex/agents').glob('*.toml')):
    data = tomllib.loads(path.read_text())
    missing = {'name', 'description', 'developer_instructions'} - data.keys()
    assert not missing, (path, missing)
    print(path, data['model'], data.get('sandbox_mode'))
PY
```

Expected: five files parse and report the intended model and sandbox default.

**Step 7: Commit the custom agents**

```bash
git add .codex/agents
git commit -m "feat: add bounded Codex agent roles"
```

### Task 5: Enable bounded project subagents

**Files:**
- Create: `.codex/config.toml`

**Step 1: Write the minimal project configuration**

```toml
[agents]
enabled = true
max_concurrent_threads_per_session = 3
```

The documented cap excludes the primary thread. This permits one Sol main thread plus at most three spawned agents. Do not set a default subagent model because every project role already specifies its own model.

**Step 2: Validate the project target**

Run:

```bash
python3 /Users/steven/.codex/vendor_imports/skills/skills/.curated/migrate-to-codex/scripts/migrate-to-codex.py \
  --validate-target ./.codex/
```

Expected: exit code 0; `.codex/config.toml`, all five custom-agent TOML files, project skills, and `AGENTS.md` are reported valid. Do not edit unrelated global Codex configuration merely to make a user-level strict-config check pass.

**Step 3: Confirm the effective project keys are discoverable**

Run:

```bash
rg -n 'enabled|max_concurrent_threads_per_session' .codex/config.toml
```

Expected: `enabled = true` and a cap of `3`.

**Step 4: Commit the project configuration**

```bash
git add .codex/config.toml
git commit -m "chore: bound Codex subagent concurrency"
```

### Task 6: Prepare the mandatory root trigger without activating it

**Files:**
- Modify: `AGENTS.md`

**Step 1: Review the compact routing section**

Prepare the following section, but do not add it to `AGENTS.md` until Task 7 Step 8:

```markdown
# Risk-Adaptive Engineering Workflow

- For every request to change, build, fix, deploy, restart, migrate, backfill, or modify behavior, use the project `risk-adaptive-engineering` skill before editing files or delegating implementation.
- Answer, explanation, and status requests remain read-only. Diagnose and review requests may use read-only delegation but do not authorize implementation.
- The skill classifies work as L0, L1, or L2. Any trading, order, position, TP/SL, attribution, risk-limit, persistent-state, concurrency/recovery, private Deepcoin write, production-action, current-position, unknown-root-cause, or unclear-impact concern is a mandatory L2 trigger.
- When uncertain, choose the higher risk level. Never downgrade an L2 hard trigger.
- Routing selects the workflow only; it does not expand user authorization, production permissions, deployment authority, or safe-window evidence.
- L1 and L2 implementation must have an independent reviewer. L2 must also have an independent tester. Validation failures return to the same builder for at most two normal repair rounds before architectural reassessment.
- Do not let multiple agents modify overlapping files concurrently. Prefer parallel read-only investigation and validation.
- The main Sol agent is the sole delegation owner. Custom workers must not spawn agents or delegate work.
- Treat custom-agent sandbox values as defaults. Dispatch read-only roles only when the parent turn's live permission context proves inherited read-only mode.
- If the parent is write-enabled, run read-only roles in a separate bundled-Codex process with `--sandbox read-only`, approval policy `never`, the repository path, configured model/effort, and the complete role contract. Stop if that boundary cannot be established.
- Use a registered named role only when the runtime exposes named-role selection; otherwise use the explicit model/effort/instructions fallback and record `EXPLICIT_ROLE_FALLBACK`.
```

**Step 2: Clarify notification ownership for validation lanes**

Add only the rule that the user-facing main Sol orchestrator sends the single
stop notification; subagents and isolated validation sessions never notify.
This permits pre-activation review without creating duplicate notifications.

**Step 3: Verify the router is still dormant**

Run:

```bash
rg -n 'Project Workflow|codex_telegram_notify|Runtime Incident AI Agent|Deepcoin API Docs' AGENTS.md
! rg -n '^# Risk-Adaptive Engineering Workflow$' AGENTS.md
```

Expected: all existing sections are found and the root routing trigger is absent.

**Step 4: Keep implicit invocation dormant**

Confirm `.agents/skills/risk-adaptive-engineering/agents/openai.yaml` still has
`allow_implicit_invocation: false`. Activation occurs only after every Task 7 gate passes.

**Step 5: Inspect the model-visible prompt**

Run:

```bash
'/Applications/ChatGPT.app/Contents/Resources/codex' debug prompt-input \
  'Classify only: update order sizing logic. Do not edit files or run tools.' \
  > /tmp/risk-adaptive-prompt-input.json
rg -n 'risk-adaptive-engineering' \
  /tmp/risk-adaptive-prompt-input.json
! rg -n 'Risk-Adaptive Engineering Workflow' /tmp/risk-adaptive-prompt-input.json
```

Expected: project skill metadata is visible but the mandatory root trigger is not. Remove the temporary output after inspection.

**Step 6: Commit the dormant configuration**

```bash
git add AGENTS.md .agents/skills/risk-adaptive-engineering .codex
git commit -m "feat: add risk-adaptive Codex team"
```

### Task 7: Forward-test routing without implementation

**Files:** None.

**Step 1: Prepare classification-only prompts**

Use these six raw requests:

```text
Correct a typo in README.md.
Fix a small isolated CSS spacing bug on a non-trading page.
Add an ordinary multi-file reporting feature with no production writes.
Change Deepcoin order sizing and leverage calculation.
Restart telegram-kol.service after updating the package.
Diagnose an intermittent current-position attribution defect; do not fix it.
```

**Step 2: Run independent read-only classification passes**

Use fresh subagents or isolated read-only sessions. Give each only the skill and risk-matrix paths plus one or two raw requests. Explicitly require classification and route selection only. Allow targeted read-only commands such as `sed` or `rg` so the agent can actually load the artifact; forbid writes, notifications, server access, and implementation.

Expected routes:

```text
README typo -> L0
isolated CSS bug -> L0 or L1 with evidence
multi-file reporting feature -> L1
order sizing/leverage -> L2
service restart -> L2
current-position attribution diagnosis -> L2 read-only investigation
```

**Step 3: Tighten only demonstrated gaps**

If a hard L2 case is downgraded, update `risk-matrix.md` and the skill instruction that allowed it, then rerun the failed case. Do not add broad duplicate prose for a case that already passes.

**Step 4: Re-run structural and strict validation**

```bash
uv run --no-project --with pyyaml python \
  /Users/steven/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/risk-adaptive-engineering
python3 /Users/steven/.codex/vendor_imports/skills/skills/.curated/migrate-to-codex/scripts/migrate-to-codex.py \
  --validate-target ./.codex/
git diff --check
```

Expected: the skill and target structure are valid and no whitespace errors are reported. This step does not prove runtime configuration loading; Step 5 does.

**Step 5: Verify the bundled runtime config layer**

Start the bundled Codex app server over stdio, initialize it, and call `config/read`
with the repository path and `includeLayers: true`. Confirm the effective response contains
`agents.enabled = true`, `agents.max_concurrent_threads_per_session = 3`, and project-file
origins for both values. Record unrelated user-level config diagnostics without changing them.

**Step 6: Smoke-test the supported dispatch path in a disposable copy**

Copy only `.codex/` and the minimum skill metadata into a disposable temporary directory;
do not copy this repository's notification instructions. Inspect the live `spawn_agent` schema.
If it exposes a named-role field, use that field and assert the child runtime context matches
`scout.toml` (Luna, medium effort, read-only, and its developer instructions).

If no named-role field exists, record that limitation and exercise the router's explicit fallback:
read `scout.toml`, spawn with `model = "gpt-5.6-luna"`, medium effort, `fork_turns = "none"`,
and include the scout instructions in the assignment. Run the parent with read-only permission,
have the disposable child attempt to create `runtime-write-must-fail`, and assert from the saved
child `turn_context` that its model is Luna, effort is medium, and sandbox is read-only. The write
must be denied and the file must remain absent. A matching `task_name` alone is never proof that
a custom TOML was loaded. Delete the disposable copy afterward.

**Step 7: Commit any forward-test correction**

If files changed:

```bash
git add .agents/skills/risk-adaptive-engineering AGENTS.md
git commit -m "test: harden risk-adaptive routing cases"
```

If no files changed, do not create an empty commit.

**Step 8: Activate routing only after every gate passes**

Append the reviewed `Risk-Adaptive Engineering Workflow` section from Task 6
to `AGENTS.md`, and change `.agents/skills/risk-adaptive-engineering/agents/openai.yaml` to:

```yaml
policy:
  allow_implicit_invocation: true
```

Commit this activation separately so rollback is immediate:

```bash
git add AGENTS.md .agents/skills/risk-adaptive-engineering/agents/openai.yaml
git commit -m "feat: activate risk-adaptive routing"
```

### Task 8: Independent review and handoff

**Files:**
- Review: `AGENTS.md`
- Review: `.agents/skills/risk-adaptive-engineering/**`
- Review: `.codex/config.toml`
- Review: `.codex/agents/*.toml`
- Review: `docs/plans/2026-08-17-risk-adaptive-agent-routing-design.md`
- Review: `docs/plans/2026-08-17-risk-adaptive-agent-routing.md`

**Step 1: Review the complete diff**

Run:

```bash
git diff HEAD~7 -- AGENTS.md .agents/skills/risk-adaptive-engineering .codex \
  docs/plans/2026-08-17-risk-adaptive-agent-routing-design.md \
  docs/plans/2026-08-17-risk-adaptive-agent-routing.md
```

Adjust the base revision if fewer commits were created. Verify that no application source, tests, secrets, production settings, or unrelated user changes are included.

**Step 2: Request independent code/config review**

Use the `requesting-code-review` skill. Require findings-first review of:

- Whether every hard L2 trigger is preserved.
- Whether any role can silently expand production authority.
- Whether reviewer/tester independence is real.
- Whether custom-agent defaults match their instructions.
- Whether the root trigger is concise enough for every project task.
- Whether rollback removes only files introduced by this feature.

Expected: PASS or actionable findings. Return findings to the implementation owner; do not have the reviewer edit files.

**Step 3: Run the final validation set**

```bash
uv run --no-project --with pyyaml python \
  /Users/steven/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/risk-adaptive-engineering
python3 /Users/steven/.codex/vendor_imports/skills/skills/.curated/migrate-to-codex/scripts/migrate-to-codex.py \
  --validate-target ./.codex/
git diff --check
git status --short
```

Expected: validators pass. `git status --short` may show pre-existing unrelated user changes, but no intended workflow file remains uncommitted.

**Step 4: Confirm no production action occurred**

Record in the final handoff:

```text
Production deployment: not required
Service restart: not performed
Deepcoin write operations: not performed
Application runtime behavior: unchanged
```

**Step 5: Push only after review and branch reconciliation**

Ensure the reviewed commits are on `codex/deepcoin-auto-trading-v1` before pushing. Do not force-push and do not discard unrelated worktree changes. This documentation/configuration-only phase does not require the server update helper or a service restart.

**Step 6: Send the single stop notification and report**

Immediately before returning control to the user, run the repository notification command once with a short non-sensitive summary. Report created files, validation evidence, selected branch/commit range, and the fact that production was untouched.
