# Runtime Agent Telegram Evidence Probe Design

## Scope

Implement the next Phase 6 playbook,
`fetch_missing_telegram_evidence`, for the production-reachable
`notification_delivery_failure` incident only. The action fetches fresh,
read-only evidence about the configured Telegram Bot API endpoint and target
operator chat. It does not send a message, fetch Telegram user messages,
download media, persist raw Telegram content, or modify a failed notification.

The existing first-pass recognition, contextual resolution, durable
notification retry, and business execution paths remain authoritative. The
handler is injected only into the separately supervised Runtime Agent and
remains dormant behind the existing Agent, action, and exact-playbook flags.

## Considered Approaches

### 1. Fetch user-message or reply evidence through the Telethon session

The main service already has a shared Telethon client and can retrieve a
missing reply. That path persists raw messages, may download media, and can
schedule authoritative recognition or contextual work. Reusing it would turn
this nominally read-only playbook into a data-ingest action and risk competing
with the live listener.

### 2. Inspect only the durable failed notification row

The Agent can already read the incident and source row from SQLite. That proves
the historical failure but does not fetch the missing evidence named by the
reviewed corpus: whether the Telegram notification endpoint has recovered.
Treating a passive row as a fresh fetch would repeat the verification flaw
avoided in earlier handlers.

### 3. Probe the Telegram Bot API through a bounded loopback endpoint

The main service already owns the dedicated bot configurations. A
loopback-only endpoint can perform the read-only Bot API methods `getMe` and
`getChat`, then return only fixed booleans. The unprivileged sidecar never
receives the bot token or chat ID. A one-shot coordinator validates the durable
incident/source relationship, invokes the endpoint, and exposes the bounded
proof once through `get_incident_summary`.

This is the selected approach. It fetches real recovery evidence without
granting the sidecar Telegram credentials or touching the live user session.

## Reachable Source Contract

The handler supports only incident type `notification_delivery_failure` and
these production source kinds:

- `runtime_incident_notification`: the source record must be an existing
  `RuntimeIncident` whose notification status is `failed`;
- `strategy_management_notification`: the source record must be an existing
  `StrategyManagementNotification` whose status is `failed`.

The source ID must be a positive integer. Corpus-only or generic source labels,
missing rows, non-failed rows, fingerprint mismatch, and every other incident
type fail closed. Context-worker and provider incidents remain
`executor_not_configured` for this handler even though the catalog permits the
playbook name.

## Main-Service Probe

The main service maps the validated source kind to one of two already loaded
configs:

- runtime incident notification → system operator bot;
- strategy-management notification → notification bot.

The endpoint accepts only an exact enum, refuses non-loopback clients and any
`X-Forwarded-For`, and permits one probe at a time. It performs `getMe` and
`getChat` concurrently with:

- fixed Telegram Bot API method names;
- the already configured bot token and chat ID;
- `trust_env=False`;
- a five-second hard HTTP timeout;
- no retries;
- no response-body logging or persistence.

The response contains only:

- `probe_complete`;
- `endpoint_reachable`;
- `bot_identity_available`;
- `target_chat_available`.

No Telegram identifiers, usernames, titles, response bodies, URLs containing
tokens, headers, or exception text leave the main process.

## One-Shot Verification

`RuntimeAgentTelegramEvidenceRefresh` validates the executor identity and live
source row before calling the endpoint. It projects the fixed booleans,
retains at most 32 in-memory captures, and atomically consumes each capture
once.

`get_incident_summary` first checks for a live capture. A successful complete
probe produces:

- `evidence_fetched: true`;
- `evidence_available: true`;
- `endpoint_reachable: true`;
- `bot_identity_available: true`;
- `target_chat_available: true`;
- evidence references `incident:<id>` and `telegram-evidence:<id>`.

If the probe completes but Telegram or the target chat is unavailable,
`evidence_available` is false and executor verification fails closed. A second
tool call returns to the ordinary passive incident projection.

## Failure, Dormancy, and Rollback

Missing configuration, invalid identity, unsupported source, busy probe,
timeout, malformed endpoint proof, or endpoint error cannot produce a verified
action. The existing executor owns idempotency, claim leases, attempt budget,
freeze behavior, and circuit breaking.

Rollback requires no schema change: clear the exact action allowlist and action
flag, stop the sidecar if it was started for a canary, and leave the normal
notification workers unchanged. The initial deployment and isolated canary
keep every persistent Agent/action flag off.

## Test and Canary Plan

Use TDD for:

- exact durable source validation for both supported notification families;
- refusal of missing/non-failed/unsupported sources and identity mismatch;
- fixed proof projection, malformed proof refusal, bounded retention, and
  one-shot consumption;
- loopback/proxy refusal, exact channel mapping, single-flight behavior,
  timeout/unavailability handling, and secret-free response shape;
- CLI handler injection and one-shot `get_incident_summary` verification;
- executor failure on incomplete or unavailable evidence.

After local regressions and review, prove a fresh production safe window.
Deploy with the sidecar and all action flags off. Run an isolated temporary-DB
canary referencing a synthetic failed notification while the read-only probe
uses the deployed loopback endpoint. Verify the production incident and
notification rows remain unchanged.
