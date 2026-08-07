# Runtime Incident Notification Watermark Design

## Problem

Production has 256 historical `severe_protection_incident` rows whose
`notification_status` is still `pending`. The notification worker currently
filters only by incident type. Adding that type to the Telegram selector would
therefore deliver the historical backlog in batches instead of canarying one
new incident.

The historical rows must remain intact for audit. They must not be rewritten as
delivered or not needed merely to make activation convenient.

## Goals

- Preserve the current notification behavior when no watermark is configured.
- Allow an operator to record the current maximum runtime-incident ID and make
  only later rows eligible for Telegram delivery.
- Keep capture, Agent diagnosis, recovery, and business execution unchanged.
- Deploy the code dormant with the production selector unchanged.
- Fail closed when a configured watermark is malformed.

## Non-goals

- Do not send a protection notification in the dormant deployment turn.
- Do not edit, delete, suppress, or reclassify the 256 historical incidents.
- Do not add time-zone or wall-clock eligibility logic.
- Do not change incident generation or fingerprint semantics.

## Chosen approach

Add the optional environment setting
`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID`. Its value is a non-negative
SQLite runtime-incident ID. When it is present, notification claims require
`runtime_incidents.id > configured_watermark` in addition to the existing
notification-status and exact-type predicates.

An absent setting preserves legacy behavior. A valid value of `0` permits all
positive incident IDs. A present but malformed, negative, or out-of-range value
maps to SQLite's signed 64-bit maximum and therefore permits no claim. This is
fail-closed without preventing the main listener service from starting.

The watermark applies only to deterministic Telegram notification claims. It
does not affect capture or Runtime Agent diagnosis.

## Components and data flow

`RuntimeIncidentConfig` gains
`telegram_notification_after_incident_id: int | None`. The configuration loader
parses the setting before returning the immutable runtime configuration.

`deliver_runtime_incident_notifications` passes the parsed watermark into
`claim_next_runtime_incident_notification`. The claim helper applies the same
eligibility predicate both when selecting the oldest row and in the compare-
and-set update. This prevents a race from claiming a row that did not satisfy
the original watermark.

No schema migration is required. Incident IDs are already monotonic primary
keys and are visible as stable operator deduplication identities.

## Dormant deployment and activation

The first deployment leaves
`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID` absent and keeps Telegram and
Agent selectors exactly `management_partial_failed`. It therefore changes no
production behavior and emits no protection notification.

In a later separately approved safe window, activation records the current
maximum runtime-incident ID, installs it as the watermark, and adds exactly
`severe_protection_incident` to the Telegram selector. Existing rows remain
pending but ineligible. Only an incident row inserted after the watermark may
be delivered as the canary.

## Error handling and rollback

Invalid configured values fail closed to the signed 64-bit maximum. An empty
value counts as invalid when the key is present.

Rollback must first restore the Telegram selector to exactly
`management_partial_failed` and restart/reload the notification worker. Only
after the severe-protection type is no longer selected may the watermark be
removed or code be reverted. Reversing that order would expose the historical
backlog.

## Verification

Tests cover absent, zero, valid, negative, malformed, empty, and overflow
configuration values. Claim tests prove that pending/failed/stale-delivering
rows at or below the watermark remain untouched, a later eligible row is
claimed, exact type filtering still applies, and the compare-and-set predicate
contains the watermark. Delivery tests prove the runtime configuration is
passed through and that no sender call occurs when only historical rows exist.

Production verification for the dormant deployment must confirm the reviewed
SHA, active service, HTTP 200, unchanged selectors, absent watermark, unchanged
historical notification states, and zero notification deliveries.
