# Message-Operation Incident Agent Gap Inventory

## Scope

This inventory is the Phase 8R.4 boundary for the operator-approved
message-operation extension. It maps the approved design to the deployed
Runtime Incident Agent before any message-path integration is added. The new
contract tables introduced in this phase are dormant persistence only: no
listener, recognizer, contextual resolver, execution worker, supervisor,
notification worker, or Agent worker imports or calls their helpers.

Status meanings:

- `existing`: reusable without a new architectural component;
- `partial`: reusable foundation exists, but the approved behavior is not yet
  complete;
- `missing`: no production component currently provides the behavior;
- `conflicting`: an existing boundary must be replaced at a later phase rather
  than extended as the final design.

## Requirement-to-Code Map

| Approved requirement | Status | Existing component to reuse | Exact gap and owning phase |
|---|---|---|---|
| Runtime incident ledger | existing | `models.RuntimeIncident` and `runtime_incidents.py` provide bounded storage, fingerprint generations, claims, diagnosis state, and notification state. | Phase 8R.6 will create message-operation incidents in this ledger; no parallel incident table is permitted. |
| Sidecar claim and diagnosis lifecycle | existing | `runtime_agent_worker.py`, `runtime_agent_contracts.py`, and `telegram-kol-runtime-agent.service` provide bounded attempts, leases, diagnosis validation, and supervision. | Phase 8R.8 will make message-operation incidents eligible through the existing lifecycle. |
| Incident-type selectors | conflicting | Configuration, `runtime_incidents.py`, `system_operator_bot.py`, and the Agent worker support exact-type allowlists and watermarks. | Exact selectors remain the safe legacy rollout mechanism, but cannot be the final eligibility boundary for every message-operation violation. Phase 8R.8 replaces final message-operation eligibility with one structurally enforced class boundary while preserving unrelated selectors. |
| Scanner observations | partial | `RuntimeIncidentObservation`, `runtime_incident_observations.py`, `runtime_incident_scanner.py`, and `runtime_incident_rules.py` provide append-only shadow observations and deterministic rules. | They scan known invariants, not one outcome contract for every executable source message. Phase 8R.5 adds contract projection; Phase 8R.6 evaluates outcomes. |
| Read-only evidence tools | partial | `runtime_agent_tools.py`, `runtime_incident_snapshot.py`, the existing projection helpers, and read-only exchange/audit handlers provide bounded incident-specific evidence. | Coverage is incident-type-specific and does not yet enforce broad OS, database, code, log, Telegram, and exchange access structurally. Phase 8R.7 adds the broker and isolation boundary. |
| Stage 1 notification | partial | `system_operator_bot.py` provides deterministic runtime-incident formatting, durable claims, retries, and Telegram delivery. | It is incident-oriented and fingerprint deduplication can suppress per-source-message alert semantics. Phase 8R.6 sends one immediate outbox alert for every violated source contract without waiting for AI. |
| Stage 2 diagnosis notification | partial | Existing Agent completion updates diagnosis fields and the system operator dispatcher can deliver bounded incident reports. | It does not guarantee a second operator-visible result for every investigation, including timeout/provider/evidence failure, and does not include a complete copyable handoff. Phase 8R.9 closes this gap. |
| Durable Codex handoff | partial | `runtime_incident_handoff.py` validates and renders reproducible bounded handoff data and CLI output. | There is no dedicated durable handoff artifact with stable ID, affected-message set, delivery lifecycle, and complete copyable prompt. Phase 8R.9 adds persistence and delivery without auto-creating a Codex task. |
| Per-message outcome coverage | missing | Raw messages, recognition decisions, contextual attempts, management envelopes/targets/items, execution ledgers, and reconciliation records are authoritative inputs. | No durable envelope states the deterministic expectation and deadline for every executable message. Phase 8R.4 adds dormant `MessageOperationContract` and `MessageOperationItem`; Phases 8R.5–8R.6 project and evaluate them. |
| Read-only OS/database/exchange enforcement | partial | Current Agent tools are closed and bounded; action authority is false; playbook allowlists are empty; exchange handlers used for diagnosis are read-only by code policy. | A dedicated unprivileged identity, query-only database/snapshots, trade-disabled exchange credential, read-only mounts, isolated workspace, and egress enforcement are not yet one audited broker boundary. Phase 8R.7 must prove these controls before broad investigation. |
| Coverage heartbeat and silent-loss monitoring | partial | The scanner service, systemd supervision, production monitor, durable observations, and independent system-operator alerting provide component health evidence. | Nothing currently proves that every eligible source message received and completed contract inspection. Phase 8R.10 adds coverage watermarks, heartbeat reconciliation, silent-loss incidents, and final audit. |

## Phase 8R.4 Additive Schema Decision

`MessageOperationContract` reuses `raw_messages.id` as its authoritative source
identity and may reference the existing `runtime_incidents.id`. Its unique key
is `(raw_message_id, policy_version)`, so retrying projection cannot create a
second contract for the same policy generation.

`MessageOperationItem` is a child of one contract. It stores only a bounded
authoritative instruction identity, sequence, expected descendant and terminal
kinds, observed terminal kind, closed status, and stable evidence references.
It does not store raw Telegram payloads, prompts, provider responses, logs,
credentials, exchange secrets, or mutation requests.

The helpers in `message_operation_contracts.py` provide only idempotent
create/get/append and compare-and-set terminal transition operations. The
schema is created by the existing additive `Base.metadata.create_all` bootstrap
in `db.py`; no existing business row is rewritten. Production integration,
projection, incident creation, notification, and Agent eligibility remain
outside Phase 8R.4.

## Dormancy and Rollback

There is no feature flag to enable in Phase 8R.4 because no production path
calls the new module. Successful deployment creates two empty additive tables.
Rollback is to run the previous application commit; the unused tables may stay
in place so rollback requires no destructive migration. A non-empty table,
new model call, notification, strategy transition, exchange request, or Agent
claim is a failed Phase 8R.4 verification.
