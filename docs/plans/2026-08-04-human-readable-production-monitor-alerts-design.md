# Human-Readable Production Monitor Alerts Design

## Problem

The production safety monitor currently sends a developer-oriented dump. Its
title is always `生产安全监控异常`, its first useful field is an internal reason
code, and version hashes are shown even when version drift is not itself a
failure. A non-technical operator cannot tell what failed, whether live trading
is affected, what to do next, or whether the message came from deterministic
system logic or the Runtime Incident AI Agent.

The current fingerprint also includes current and expected Git heads whenever
another abnormality is present. Frequent deployments can therefore make an
unchanged historical `audit_abnormal` result look new and trigger another
notification even though version drift is deliberately not a monitor failure.

## Scope

Use a phased rollout. This phase changes only notifications emitted by the
independent production safety monitor. Runtime incident, strategy management,
position protection, and AI diagnosis notifications remain unchanged until the
first phase is verified in production.

The phase does not change trading decisions, Deepcoin writes, database schema,
monitor safety evaluation, or Runtime Agent authority.

## Chosen Approach

Use deterministic Chinese templates derived only from allowlisted monitor
reason codes and validated structured details. Do not ask an AI model to assign
severity, infer impact, choose operator actions, or write the notification.

Each notification must answer, in order:

1. What happened?
2. What is the known or unknown impact on current trading?
3. Is immediate action required?
4. What exactly should the operator do or avoid doing?
5. Did deterministic system logic or the AI Agent produce the notification?

Internal codes, validated batch references, and the check time belong in a
final diagnostic section. Git hashes are omitted unless a future dedicated
deployment-integrity reason makes them directly relevant.

## Severity Model

The formatter uses three operator-facing levels:

- `critical` / `🔴 立即处理`: the service is unavailable, an approved live
  setting drifted, an exchange outcome is unknown, a duplicate exact close is
  suspected, or a critical monitor input cannot be verified.
- `review` / `🟡 稍后核查`: actionable management history, recent journal
  errors while the service remains active, or damage limited to the monitor's
  own notification state.
- `recovered` / `🔵 状态提醒`: a previously recorded monitor abnormality has
  been fully rechecked and is now healthy.

When multiple reasons are present, the highest severity controls the title,
impact statement, and safest operator action. The body shows at most three
plain-language problems and then states how many additional problems exist.

`audit_abnormal` is normally `review`, but becomes `critical` when its validated
per-state counts include `submit_unknown`. Completed `terminal_blocked` history
remains visible in the audit report and never causes a notification.

## Reason Mapping

Critical mappings:

- `service_inactive`: the automatic trading service is not running normally.
- `auto_trade_enabled_drift`: the automatic-trading switch differs from the
  approved setting.
- `management_execution_mode_drift`: the position-management mode differs from
  the approved setting.
- `max_concurrent_positions_drift`: the per-group position limit differs from
  the approved setting.
- `event_unknown_status`: a submitted exchange action has no confirmed result.
- `event_recovery_status`: a position-management action requires recovery.
- `duplicate_manual_close`: the same exact position may have received a
  duplicate close request.
- `adapter_failure`: the monitor could not read a critical source.
- `audit_incomplete`: management-history verification did not complete.
- `malformed_snapshot`: a safety input had an invalid shape or value.

Review mappings:

- `audit_abnormal`: one or more actionable historical management records lack
  enough evidence to be terminal.
- `journal_errors`: the running service wrote recent error-level journal
  entries and their operational impact is not yet known.
- `state_invalid`: the monitor's own notification state was invalid or rebuilt;
  this does not claim that the trading database is damaged.

An unknown or unmapped reason fails closed to a critical generic explanation:
the safety check found a problem it could not explain, current production
safety cannot be confirmed, and the operator must avoid duplicate order or
close actions until a developer checks the monitor.

## Notification Contract

The renderer consumes a structured presentation rather than concatenating raw
monitor fields:

```text
MonitorAlertPresentation
  severity
  title
  problems[0:3]
  impact
  operator_action
  source_statement
  checked_at_shanghai
  technical_codes
  actionable_batch_refs[0:10]
  additional_problem_count
  additional_batch_count
```

Every string is selected from fixed templates. Only checked time, bounded
non-negative counts, fixed tokens, and validated redacted references may be
interpolated. Raw exceptions, journal text, Telegram content, exchange
requests/responses, environment variables, and credentials are forbidden.

The current production case should render as:

```text
【🟡稍后核查：2条历史交易管理记录无法确认】

发生了什么：
系统发现两个过去的仓位管理任务缺少足够证据，无法确认它们当时是否完整结束。

当前影响：
没有检测到交易服务停止或设置变化。仅凭现有历史资料，无法进一步确认这些记录是否影响过对应仓位。

你需要做什么：
不需要立即操作，也不要手动重复平仓。安排开发者核查管理批次 17、22；状态不变时不会重复提醒。

通知来源：
系统定时安全检查，不是 AI Agent。

排查信息：
检查时间：2026-08-04 09:00（北京时间）
技术代码：audit_abnormal
```

The renderer preserves the existing maximum message length. If truncation is
necessary, it removes additional diagnostic references first; it must preserve
the title, impact, and operator-action sections.

## Audit Projection

The read-only management audit will add a dedicated bounded actionable summary
instead of relying on the existing recent-batch list. It contains:

- counts for `blocked`, `partial_failed`, `recovery_required`, and
  `submit_unknown`;
- at most ten validated actionable batch references and their effective
  actionable states;
- the total actionable count and an explicit truncation flag.

The selection must cover actionable state on either the batch or any leg, use
the same exclusions as the existing abnormal count, remain read-only, and use a
stable deterministic order. A truncated list renders as `共 N 个，仅展示前 10
个`. Missing, malformed, or incomplete actionable-reference evidence never
reduces severity or hides the total; it adds `audit_incomplete` or
`malformed_snapshot` through the existing fail-closed evaluation.

## Fingerprinting And Notification State

The fingerprint covers only operator-relevant, validated facts:

- reason codes and derived severity;
- relevant setting/service values for the reasons that use them;
- actionable audit counts, states, and displayed references;
- bounded event and journal counts.

It excludes check time and Git heads when no deployment-integrity reason is
present. A normal deployment therefore cannot retrigger unchanged historical
audit residue.

Critical anomalies notify immediately when new or changed and remind after six
hours while unchanged. Review-only historical audit residue notifies once per
meaningful fingerprint and then stays log-only. Count, state, batch-set, or
severity changes notify immediately.

The monitor state gains an optional, validated previous-reason list so a blue
recovery notice can be sound. Legacy four-field state remains readable and does
not produce a speculative recovery notice. Service and setting reasons may be
declared recovered after their normal complete recheck. An audit reason may be
declared recovered only after a new full, complete, healthy audit; a run that
skips the audit cannot clear it for notification purposes.

## Error Handling And Security

- Presentation building is pure and deterministic.
- Unknown codes use the critical generic fallback.
- Invalid interpolated values render as unavailable and cause the existing
  fail-closed monitor reason; they are never copied raw.
- Formatting or Telegram delivery failure never changes trading behavior.
- A failed delivery does not persist successful-delivery dedupe state.
- The source statement always says the independent system timer produced the
  alert and that the AI Agent did not classify or write it.
- The independent monitor remains read-only against the production checkout and
  database and retains its current systemd sandbox.

## Verification

Test-driven coverage must prove:

- every fixed reason has one deterministic Chinese mapping;
- severity precedence and three-problem bounds;
- `audit_abnormal` is review-only except when `submit_unknown` is present;
- the two known historical batches render the approved example;
- actionable batch references are complete, bounded, stable, and safely
  truncated;
- deployments and unrelated Git-head changes do not alter an audit-only
  fingerprint;
- meaningful audit changes and severity escalation do alter the fingerprint;
- audit recovery is not announced unless a new complete audit passed;
- legacy state is compatible and produces no speculative recovery;
- unknown reasons, malformed fields, newline injection, secret-shaped strings,
  huge integers, and oversized messages fail safely;
- delivery failure remains retryable;
- monitor evaluation, database read-only behavior, and trading paths are
  unchanged.

Production rollout follows the repository safe-window workflow. Pause the
monitor timer, deploy the reviewed commit, synchronize its expected-head
configuration, run a no-notify production diagnostic, inspect a simulated
delivery, restore the timer, and verify the main service, loopback health, and
monitor state. No Deepcoin write, trading-setting change, or management-history
mutation is permitted.

## Rollback

Revert the formatter, audit projection, and optional state extension through the
normal Git deployment workflow. The state reader remains backward compatible,
so rollback does not require deleting monitor state. No trading database schema
or exchange state changes, and no transaction history needs rollback.
