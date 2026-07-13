# AI Prompt Registry

## Production contract

Every network AI call must obtain its business prompt from the database-backed registry. Provider URLs, model selection, timeouts, and API keys remain in `config/ai_recognition.yaml`; API keys are never returned by Web APIs or rendered into HTML.

Stable prompt IDs:

- `trading.analysis.shared`: shared trading template A, including new-strategy judgment, position tracking, and lifecycle events.
- `trading.analysis.mimo_vision`: MiMo-only image template B.
- `research.chat.system`: global Web research-chat system prompt.
- `research.chat.group`: optional per-Telegram-group research prompt.
- `strategy.alert.classifier`: strategy-alert classification template.
- `trading.disagreement.semantic_review`: DeepSeek-only independent semantic review used after MiMo automation completes.

Runtime composition is fixed:

- DeepSeek text validation: `A + C`.
- MiMo authoritative recognition: `A + B + C`.
- Background disagreement review: the published `trading.disagreement.semantic_review` prompt plus a bounded runtime packet containing the current message, safe active-strategy context, persisted MiMo result, and persisted automation outcome.
- `C` is runtime message, image, recent-message, and active-strategy context. It is not an editable prompt version.

MiMo is authoritative for both text and image messages. Its successful decision and exact prompt versions are saved as unclaimable `execution_pending` before MiMo-driven stop-loss, take-profit, or exit processing continues. Immediately before any lifecycle mutation or auto-trade call, that exact generation must atomically claim durable ownership by moving `execution_pending -> execution_running`. A newer recognition may supersede an unclaimed pending generation, but it cannot overwrite a running generation; a stale generation whose claim loses performs no mutation or auto-trade. Only the running generation may atomically persist its automation outcome and transition to semantic-review `pending` (or remain `completed` when an unchanged comparison is already complete). A post-submit persistence failure deliberately leaves `execution_running`, blocking duplicate execution until recovery. DeepSeek auxiliary comparison is performed later by the Web service semantic-review worker; it cannot delay or override authoritative execution. A MiMo transport or schema failure remains fail-closed, uses its separate failure alert, and is not queued for semantic review; its terminal audit save is also CAS-guarded and cannot clear or overwrite an existing `execution_running` owner.

## Semantic-review publication and audit contract

The semantic-review prompt is versioned independently from trading templates A and B. Its validation profile is `semantic_disagreement_review`; publication rejects a draft that removes the strict JSON fields, closed action/severity/conflict enums, independent-current-message interpretation, direct-evidence rule, no-trade-mutation boundary, image-pixel limitation, or JSON-only/no-extra-fields directives. Each call records the exact prompt version in `ai_prompt_invocations` and merges that version into `recognition_decisions.prompt_versions_json` only when the claimed review completes.

The Web service owns the database-backed worker. Review state proceeds `pending -> running -> completed`, with a maximum of three attempts and an increasing retry delay; exhausted work becomes `failed`. A five-minute-old `running` claim is recoverable, and token-guarded completion prevents the stale worker from overwriting the new owner. Neither retry nor failure touches `automation_status` or `automation_reason`.

Only final `critical` severity can claim a notification. The worker persists an immutable `notification_payload_json`, its SHA-256 `notification_fingerprint`, and `notification_status = scheduled` before calling the bot. A successful call becomes `sent`; a delivery exception becomes terminal `failed`. Existing `scheduled`, `sent`, or `failed` claims are not automatically resent, which is the deliberate at-most-once boundary. `none` and `normal` never claim notification state. Historical completed rows lacking `disagreement_severity` are presented by the Web UI as `unclassified` / `待重新复核`, not as agreement.

## Editing lifecycle

The Web prompt center uses `draft -> validate -> historical test -> publish`. Saving a draft never changes the active version. Draft saves and publication use optimistic version checks, and publication requires a non-empty change note. Editing an already validated draft clears its validation state and deletes historical comparisons for that mutable draft.

Trading publication accepts only tests run against the currently active companion prompt versions: A requires successful MiMo and DeepSeek coverage, while B requires MiMo coverage. B cannot be tested with DeepSeek. A later A/B publication makes older comparison baselines ineligible for publication.

Historical comparisons call the model with the active and draft prompt separately and store results in `ai_prompt_test_runs`. They must never call authoritative apply, automatic trading, lifecycle mutation, execution binding, or strategy notification paths.

Publishing atomically supersedes the old active version. Rollback copies a selected old published version into a new auditable version; it does not change or roll back application code.

## Persistence and audit

- `ai_prompt_definitions`: stable identity, scope, consumers, variables, and validation profile.
- `ai_prompt_versions`: active, draft, superseded, validation, publication, and rollback history.
- `ai_prompt_test_runs`: isolated active-versus-draft comparisons.
- `ai_prompt_invocations`: exact prompt version IDs used by live AI calls.
- `recognition_decisions.prompt_versions_json`: MiMo and DeepSeek versions for authoritative decisions.

The database active version always wins over legacy YAML prompt fields. YAML fields are compatibility seed inputs only and must not be reintroduced at runtime call sites. The legacy model/provider endpoint ignores and omits prompt fields, so the registry is the sole Web prompt authority. Remove the YAML seed fallback only after every deployed database has seeded definitions and a backup/restore procedure has been verified.

## Deployment and rollback

Develop locally, run focused and full checks, then push the reviewed commit to `codex/deepcoin-auto-trading-v1`. Update production with `scripts/server_git_update.ps1` or the documented shell helper so the server pulls GitHub, reinstalls the editable package, and restarts `telegram-kol.service`.

After deployment verify the server commit, service state, logs, prompt list, A/B composition, semantic-review prompt validation/version audit, isolated test behavior, next-call prompt version, rollback, critical-only notification, normal Web visibility, Fengge-style exit lifecycle, and Deepcoin reconciliation.

For an unsafe prompt, use prompt rollback first. For a code defect, deploy a code revert separately. Prompt rollback cannot repair application code.

## Deployment evidence

Production deployment for semantic disagreement review is not yet recorded. After production verification, record the deployed commit, local test counts, server `HEAD`, service state, seeded definition/version IDs, semantic-review database counts, controlled latency result, and retained legacy fallback here.
