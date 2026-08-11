# MiMo v2 Analysis and Observability Design

Date: 2026-08-11

Status: approved

## Goal

Make the first authoritative MiMo analysis the primary, auditable result on
each Web message card without weakening or bypassing the existing automatic
trading safety path.

The design must:

- represent new strategies, position management, exits, cancellation,
  revisions, informational messages, and uncertain messages explicitly;
- separate model-call success from business intent and from execution outcome;
- persist structured MiMo call attempts and terminal errors;
- preserve per-image MiMo evidence for later text-only contextual analysis;
- display the same canonical MiMo result that feeds automatic trading;
- avoid Web-side parsing of free-form model explanations;
- keep the production critical path to one normal MiMo call per message;
- avoid production Shadow dual calls because they could delay follow-up
  instructions for existing positions;
- retain the existing candidate, lifecycle, safety-gate, idempotency, binding,
  risk, and Deepcoin execution path.

## Current problems

The current top-level `recognition_result` is limited to `是策略`, `非策略`,
and `识别失败`. A valid position-management decision can therefore be displayed
as `非策略` even when `lifecycle_event.event_type` is `position_update`.

Runtime and semantic states are also mixed. A successful MiMo interpretation
that cannot be applied to one safe lifecycle currently appears as a generic
recognition failure. Internal request attempts are collapsed into error text,
and the Web page does not expose the separately persisted MiMo image evidence.

The current execution path consumes a compatibility mixture of:

- `recognition_result`;
- `strategy`;
- `lifecycle_event`;
- the bounded `instructions` list;
- accepted `SignalCandidate` rows;
- durable `MessageInstructionItem` rows.

The `instructions` rollout is independently controlled by
`multi_instruction_mode` and a future-message watermark. It cannot be replaced
directly without risking a change to live execution.

## Design principles

1. There is one canonical MiMo v2 semantic result.
2. Web and execution consume the same v2 intents and evidence.
3. A deterministic adapter, not a second AI interpretation, feeds the current
   execution compatibility path.
4. System runtime facts are not model claims.
5. The Web must not infer meaning from punctuation, keywords, or free-form
   explanations.
6. Model interpretation, contextual resolution, system acceptance, and
   exchange outcome remain separately visible.
7. A normal production message makes one MiMo call.
8. Any fallback to v1 is allowed only before an execution side effect.
9. No production Shadow dual-call mode is introduced.
10. Rollout and rollback are future-only and never replay prior messages.

## MiMo v2 canonical contract

The v2 payload uses one bounded `intents` list for both display and execution
input.

```json
{
  "contract_version": "mimo-authoritative-v2",
  "summary": "管理已有 ETH 空单并移动止损",
  "confidence": 0.94,
  "intents": [
    {
      "intent_type": "position_management",
      "action": {
        "kind": "move_stop_to_protect",
        "target": {
          "lifecycle_id": 790,
          "thread_id": 52
        },
        "strategy": null,
        "parameters": {
          "stop_loss": "1940"
        }
      },
      "reason": "消息明确要求移动止损到1940",
      "confidence": 0.95,
      "evidence_refs": [
        "text:stop_loss",
        "image:381:symbol",
        "image:381:side"
      ]
    }
  ],
  "evidence": {
    "text": {
      "observed_text": "移动止损到1940",
      "fields": {}
    },
    "images": [
      {
        "asset_id": 381,
        "image_type": "position_screenshot",
        "quality": "clear",
        "observed_text": "ETHUSDT 永续，空，止损1940",
        "summary": "ETHUSDT空仓持仓截图",
        "fields": {
          "symbol": {
            "value": "ETH",
            "source": "image",
            "confidence": 0.99
          },
          "side": {
            "value": "short",
            "source": "image",
            "confidence": 0.98
          },
          "stop_loss": {
            "value": "1940",
            "source": "image",
            "confidence": 0.96
          }
        },
        "confidence": 0.97
      }
    ],
    "conflicts": []
  }
}
```

### Intent types

The closed intent enum is:

- `new_strategy`;
- `position_management`;
- `exit`;
- `cancel_entry`;
- `strategy_revision`;
- `entry_context`;
- `position_report`;
- `market_commentary`;
- `non_trading`;
- `unclear`.

One message may contain multiple independent intents. The model must not erase a
management or cancellation intent because the same message also contains a new
entry.

### Action kinds

Executable candidate action kinds remain bounded to the existing supported
vocabulary, including:

- `entry`;
- `cancel_pending_entry`;
- `replace_entry`;
- `full_exit`;
- `partial_exit`;
- `partial_take_profit`;
- `move_stop_to_protect`;
- `hold_update`;
- `risk_update`.

Informational intents have no executable action. The model cannot authorize a
trade merely by calling an intent actionable; only a supported, valid action
can enter the deterministic adapter and all existing safety gates.

### Evidence references

Every actionable intent should identify its supporting text or image fields.
References are auditable provenance and never replace exact target, price,
side, or binding validation.

## Runtime facts

Call success, attempts, fallback, and execution are system facts. They are not
fields MiMo may self-report.

Each complete run records:

- contract version;
- model and prompt versions;
- input kind and evidence/input fingerprint;
- run kind (`v1_authoritative`, `v2_authoritative`, or `v1_fallback`);
- start and completion times;
- terminal status;
- attempt count;
- retry-of relationship for a later whole-run retry;
- final error code and sanitized error;
- whether the result became authoritative;
- canonical payload and projection fingerprints.

Each provider attempt records:

- run identifier and ordinal;
- start and completion times;
- latency;
- success, timeout, HTTP failure, invalid JSON, or contract failure;
- sanitized error code and message;
- response fingerprint;
- whether that response became the selected valid result.

Suggested additive tables are `mimo_recognition_runs` and
`mimo_recognition_attempts`. Authoritative result persistence must remain
durable before execution. Observability-only attempt detail must not create a
second execution authority.

## Per-image evidence

MiMo already separates text and image evidence in
`MessageEvidenceVersion.text_evidence_json` and
`MessageEvidenceVersion.image_evidence_json`. V2 enriches each image record
with bounded `quality`, `observed_text`, and `summary` fields while retaining
the existing `asset_id`, `image_type`, `fields`, and confidence.

Each image remains independently attributable. Text and image evidence are not
silently merged. Conflicts are explicit. The JSON stores no image base64,
credentials, or complete exchange response.

When v2 is authoritative, its text and image evidence populate the current
immutable evidence version. Later DeepSeek contextual analysis consumes the
saved structured evidence as text and does not reread pixels.

## Deterministic v2 compatibility adapter

The adapter maps v2 intents into the current execution compatibility structure:

| V2 action | Existing compatibility view |
| --- | --- |
| `entry` | `recognition_result=是策略` plus `strategy` |
| `move_stop_to_protect` | `lifecycle_event=position_update` |
| `full_exit` | `lifecycle_event=exit_position` |
| `cancel_pending_entry` | `lifecycle_event=cancel_entry` |
| `replace_entry` | current revision structure |
| no action | no executable candidate |

The adapter is a pure function. It may copy, normalize closed enums, and reject
invalid structures. It may not:

- parse source message text or model reasons;
- infer from punctuation or keywords;
- inspect image pixels;
- guess symbol, side, price, lifecycle, or thread;
- fill a missing target or parameter;
- increase confidence;
- query Deepcoin;
- bypass candidate, ownership, binding, idempotency, risk, or execution gates.

The existing candidate/lifecycle projection and Deepcoin path remain unchanged
behind the adapter during this project. Native v2 execution, if ever pursued,
is a separate future project.

## Production call and fallback flow

There is no production Shadow mode.

Normal flow:

1. Receive and persist one Telegram message and media.
2. Call MiMo v2 once.
3. Validate and canonicalize the v2 payload.
4. Persist the authoritative payload and evidence.
5. Deterministically adapt v2 to the current execution compatibility view.
6. Claim the authoritative generation.
7. Run the existing candidate/lifecycle, safety, and Deepcoin path.
8. Persist real automation outcome.
9. Run DeepSeek semantic review later and off the critical path.

V1 fallback is exceptional. It is allowed only when v2 has a transport,
invalid-JSON, contract, or adapter failure before any execution side effect.
Fallback is invoked at most once for that message.

Fallback is forbidden after any of the following:

- execution ownership has produced a mutable execution state;
- a potentially executable durable instruction has been created;
- a lifecycle mutation has been applied;
- a Deepcoin request has been attempted;
- the exchange result is unknown;
- a submitted result failed to persist.

After a possible write, the only valid path is reconciliation or manual
recovery. V1 must never re-recognize and execute the same message.

## Circuit breaker

The system automatically restores v1 for future messages when:

- one v2 contract-validation or adapter failure occurs; or
- three consecutive v2 transport/HTTP/invalid-response failures occur.

Business outcomes such as non-trading, low confidence, ambiguous target, or a
safety-gate refusal do not count as technical v2 failures.

The breaker never replays processed messages, deletes evidence, modifies
submitted exchange work, or retries an unknown exchange outcome. It emits one
deduplicated critical operator alert.

## Web message card

The message card order is:

1. Telegram text and media.
2. MiMo first-pass analysis, always prominent.
3. Per-image MiMo evidence, expanded when images exist.
4. Contextual second-stage decision, only when it ran.
5. System acceptance and automatic execution truth.
6. DeepSeek auxiliary semantic review, collapsed by default.

### MiMo runtime header

Display:

- success, failure, or v1 fallback;
- model and contract version;
- input kind;
- attempt and whole-run retry counts;
- total duration;
- final error code and reason;
- which result became authoritative.

### MiMo first-pass result

Render every intent in order. Do not choose a main intent. Display:

- intent label from a static enum translation;
- concrete action kind;
- target lifecycle/thread;
- strategy and action parameters;
- confidence;
- MiMo reason;
- evidence references.

### Image result

For each image display:

- thumbnail;
- image type and quality;
- observed text and summary;
- structured fields and per-field confidence;
- image confidence;
- text/image or cross-image conflicts;
- a default-collapsed raw evidence JSON view.

Missing values remain missing. Web code does not extract or reconstruct them.

### Context and execution separation

The Web must distinguish:

- what MiMo first saw;
- what contextual resolution linked, supplemented, held, or left unresolved;
- which intents the system accepted or rejected;
- which Deepcoin action was attempted and confirmed.

This permits an accurate sequence such as:

```text
MiMo run: succeeded
MiMo intent: position management / move stop
System acceptance: failed to resolve one safe lifecycle
Automatic trading: not executed
```

A projection failure must not be relabeled as a MiMo provider failure.

### Historical v1 compatibility

Historical messages are not re-run through AI. The Web labels them `MiMo
历史结果 · v1格式`, shows reliable existing fields and image evidence, and
states when attempt or per-image detail was not recorded. It does not invent a
v2 intent list.

### Prohibited Web behavior

Web code may parse JSON, map closed enums to Chinese labels, format values, and
join an `asset_id` to a thumbnail. It may not parse message text or model
reasons, use regexes to extract trade values, select a primary intent, choose a
target strategy, fill missing image fields, or reinterpret history with current
settings.

## Validation strategy without production Shadow

V2 is validated on the server using an isolated replay command because the
real MiMo configuration and Telegram media are available only there.

The replay:

- reads bounded production messages and media;
- writes only a temporary database or isolated artifact directory;
- never enters the live listener;
- never writes production candidate, lifecycle, instruction, or execution
  tables;
- never calls a Deepcoin write endpoint;
- never sends a notification.

The corpus includes all existing recognition regressions, known incidents,
recent management and exit messages, current-position follow-up forms, text and
image strategies, position screenshots, blurry/conflicting images, multi-action
messages, ambiguous targets, and ordinary non-trading messages.

There may be no unsafe mismatch:

- no v1 safe action omitted by v2;
- no unsupported new executable action;
- no symbol, side, target, entry, stop, take-profit, or fraction drift;
- no partial/full exit confusion;
- no cancellation/close confusion;
- no non-trading message promoted to execution;
- no lost sibling action;
- no incorrect text/image source attribution.

Wording-only and non-executable classification differences may be manually
accepted and recorded.

## Performance gates

Replay benchmarks v1 and v2 on the same server and bounded corpus.

Required gates:

- adapter P95 below 50 ms;
- v2 end-to-end P95 no more than 115% of v1 P95;
- bounded image text, field count, evidence references, and response size;
- no DeepSeek or Web work on the live execution critical path.

If v2 exceeds the gate, reduce response verbosity before production enablement.

## Automated tests

Tests cover:

- every intent and action type;
- multi-intent order and duplicate rejection;
- invalid enums, targets, strategies, parameters, and evidence references;
- pure adapter determinism;
- candidate and instruction equivalence;
- lifecycle, binding, risk, order-draft, idempotency, and Deepcoin request
  equivalence with recorded/fake clients;
- first-pass versus contextual result display;
- independent per-image evidence;
- structured attempt and retry history;
- v1 fallback only before side effects;
- no fallback after claim/mutation/submit/unknown outcome;
- at-most-once fallback;
- circuit breaker behavior;
- historical v1 compatibility;
- Web absence of free-form semantic parsing.

## Production enablement

The production setting has only:

- `v1`;
- `v2_live_adapter`.

It also retains a future-message activation watermark, automatic v1 fallback,
and the circuit breaker.

Enablement requires:

- all local tests passing;
- isolated server replay passing;
- v2 prompt version fixed and v1 prompt available for fallback;
- successful additive schema migration;
- no `executing`, `submitted`, `unknown`, or `recovery_required` instruction;
- no active recovery or exchange reconciliation;
- no time-sensitive strategy operation in progress;
- recorded future-message watermark;
- tested rollback switch.

Existing open positions do not have to be closed, but activation and service
restart cannot occur while an operation is in flight.

## Rollback

Rollback sets `mimo_contract_mode=v1` for future messages only.

Rollback never:

- replays messages at or before the watermark;
- deletes v2 runs or evidence;
- deletes candidates, instructions, lifecycle rows, or exchange records;
- reverses confirmed trades;
- retries unknown Deepcoin outcomes.

## Completion criteria

The work is complete only when:

- every new message displays the complete first-pass MiMo result;
- position management is not presented merely as non-strategy;
- MiMo failure, reason, attempts, and fallback are traceable;
- per-image evidence is saved and displayed;
- first-pass, context, system acceptance, and execution truth are distinct;
- a normal production message makes one MiMo call;
- existing automatic trading safety gates remain authoritative;
- v2 technical failure can fall back before any side effect;
- v2 and v1 cannot both execute the same message;
- rollback is future-only and preserves all audit/exchange evidence.
