# Runtime Agent Dedicated MiMo Provider Design

## Goal

Give the Runtime Incident AI Agent its own MiMo credentials so its provider
usage can be isolated in the MiMo console without adding an application token
ledger or changing the existing authoritative recognition provider.

## Decision

The Runtime Agent will load a dedicated, fail-closed provider configuration:

- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_BASE_URL`
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_API_KEY`
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_MODEL`
- `TELEGRAM_KOL_RUNTIME_AGENT_LLM_TIMEOUT_SECONDS`

The production model remains `mimo-v2.5` at the direct MiMo
OpenAI-compatible endpoint. The dedicated API key is a server secret and must
never be committed, persisted in SQLite, included in a handoff, or emitted in
logs.

## Architecture

`load_runtime_agent_llm_config` will read only the dedicated Runtime Agent
variables from the existing root-owned
`config/runtime_incident_agent.env`. It will not inherit
`TELEGRAM_KOL_LLM_*`, the authoritative recognition model configuration, or
any other provider credential.

Both `runtime-incident-agent-once` and
`runtime-incident-agent-worker` will use this dedicated loader. When the Agent
feature flag is enabled, incomplete credentials will fail before any incident
is claimed or provider request is made. This avoids consuming the durable
attempt budget because of a deployment configuration error and prevents a
silent fallback to another API key.

The existing bounded OpenAI-compatible structured tool-call transport remains
unchanged. The application will continue to discard provider usage metadata;
token accounting is intentionally handled only by the MiMo console through
the dedicated key.

## Security and Operations

The key will be installed only on the production server in
`config/runtime_incident_agent.env`, owned by root and mode `0600`. Tests and
documentation use placeholders only. Verification output may report whether
the configuration is complete, plus the selected endpoint host and model, but
must never print the key or authorization header.

Initial deployment keeps the sidecar disabled and all action flags and
allowlists empty. After code deployment and the normal continuity gate, a
bounded provider-only probe will verify MiMo authentication and structured
tool-call compatibility without claiming an incident or enabling an action.
Only after that proof may the existing reviewed incident be used for the
single reversible Phase 6 canary.

## Failure and Rollback

- Missing or invalid dedicated configuration fails closed.
- Provider authentication, timeout, schema, or tool-call incompatibility does
  not fall back to another provider.
- The sidecar and action authority remain disabled during initial rollout.
- Rollback stops and disables the sidecar, clears the Agent enable flag, and
  removes the dedicated provider values from the root-owned environment file.
- Existing Telegram intake, authoritative MiMo recognition, contextual
  resolution, strategy management, and trading paths remain unchanged.

## Verification

Local tests cover dedicated loading, strict isolation from shared credentials,
missing-configuration refusal before claim, CLI wiring, redaction, and the
existing Runtime Agent regression suites.

Server verification checks file ownership and mode without printing contents,
confirms the selected MiMo model and endpoint host with the key redacted, runs
one bounded provider compatibility probe, and rechecks service/listener and
business-mutation continuity before any canary.
