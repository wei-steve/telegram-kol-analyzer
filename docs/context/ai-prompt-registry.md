# AI Prompt Registry

## Production contract

Every network AI call must obtain its business prompt from the database-backed registry. Provider URLs, model selection, timeouts, and API keys remain in `config/ai_recognition.yaml`; API keys are never returned by Web APIs or rendered into HTML.

Stable prompt IDs:

- `trading.analysis.shared`: shared trading template A, including new-strategy judgment, position tracking, and lifecycle events.
- `trading.analysis.mimo_vision`: MiMo-only image template B.
- `research.chat.system`: global Web research-chat system prompt.
- `research.chat.group`: optional per-Telegram-group research prompt.
- `strategy.alert.classifier`: strategy-alert classification template.

Runtime composition is fixed:

- DeepSeek text validation: `A + C`.
- MiMo authoritative recognition: `A + B + C`.
- `C` is runtime message, image, recent-message, and active-strategy context. It is not an editable prompt version.

MiMo is authoritative for both text and image messages. DeepSeek is auxiliary on text messages only. A disagreement creates an operator notification but does not wait for manual review and does not block MiMo-driven stop-loss, take-profit, or exit processing.

## Editing lifecycle

The Web prompt center uses `draft -> validate -> historical test -> publish`. Saving a draft never changes the active version. Editing an already validated draft clears its validation state. Trading prompts additionally require at least one completed isolated historical comparison before publication.

Historical comparisons call the model with the active and draft prompt separately and store results in `ai_prompt_test_runs`. They must never call authoritative apply, automatic trading, lifecycle mutation, execution binding, or strategy notification paths.

Publishing atomically supersedes the old active version. Rollback copies a selected old published version into a new auditable version; it does not change or roll back application code.

## Persistence and audit

- `ai_prompt_definitions`: stable identity, scope, consumers, variables, and validation profile.
- `ai_prompt_versions`: active, draft, superseded, validation, publication, and rollback history.
- `ai_prompt_test_runs`: isolated active-versus-draft comparisons.
- `ai_prompt_invocations`: exact prompt version IDs used by live AI calls.
- `recognition_decisions.prompt_versions_json`: MiMo and DeepSeek versions for authoritative decisions.

The database active version always wins over legacy YAML prompt fields. YAML fields are compatibility seed inputs only and must not be reintroduced at runtime call sites. Remove the fallback only after every deployed database has seeded definitions and a backup/restore procedure has been verified.

## Deployment and rollback

Develop locally, run focused and full checks, then push the reviewed commit to `codex/deepcoin-auto-trading-v1`. Update production with `scripts/server_git_update.ps1` or the documented shell helper so the server pulls GitHub, reinstalls the editable package, and restarts `telegram-kol.service`.

After deployment verify the server commit, service state, logs, prompt list, A/B composition, isolated test behavior, next-call prompt version, rollback, MiMo disagreement notification, Fengge-style exit lifecycle, and Deepcoin reconciliation.

For an unsafe prompt, use prompt rollback first. For a code defect, deploy a code revert separately. Prompt rollback cannot repair application code.

## Deployment evidence

Record the deployed commit, local test counts, server `HEAD`, service state, seeded definition/version IDs, and retained legacy fallback here after production verification.
