# Bot Notification Routing Design

## Goal

Separate human strategy-expiry decisions from informational system notifications,
and suppress alerts for Telegram events that contain neither readable text nor an
image. Both changes preserve the existing fail-closed trading behavior and
durable audit trail.

## Roles

The existing strategy-alert bot remains unchanged: it forwards new KOL strategy
signals.

The existing system-operator bot becomes the decision bot. It continues to use
the existing chat ID and is restricted to pending-entry expiry reviews and their
interactive callback buttons: continue waiting, cancel the expired pending
order, or retain it.

The new notification bot uses the supplied token and the same chat ID. It sends
all other system notifications, including authoritative AI-recognition failures
or disagreements, semantic-review incidents, position-attribution incidents,
strategy-management incidents, instruction summaries, and operational notices.
It never accepts or executes a trading decision callback.

## Configuration and Routing

Keep the current `TELEGRAM_KOL_SYSTEM_BOT_*` environment variables as the
decision-bot configuration for backward compatibility. Add separate
`TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN`,
`TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID`, and optional timeout configuration for
the notification bot. Production will set the notification chat ID to the
existing system-bot chat ID.

The application startup wiring will pass the decision configuration only to the
expiry-review sender and command poller. Notification-producing paths will use
the new notification configuration. Disabled notification configuration must
only suppress delivery; it must not affect decision processing or auto-trading.

## Empty-input Recognition Failures

When authoritative MiMo returns the deterministic error
`message has no readable text or image`, the system will record
`notification_status="suppressed_empty_input"` and will not send a Telegram
notification. It still persists the failed recognition decision and the
automation result `skipped/mimo_authoritative_failed`; no candidate, order, or
position mutation is permitted.

The suppression is deliberately exact. Messages with text, declared media whose
file cannot be read, model timeouts, malformed model output, and all other
authoritative failures continue to notify through the notification bot.

## Verification

Focused tests will prove: expiry review buttons continue to use the decision
bot; recognition and operational notifications use the notification bot; empty
input has a durable suppressed audit status and no outbound delivery; and a
textual management instruction with a MiMo failure still produces a notification
and remains fail-closed. Local tests validate logic; deployment verification
runs on the server because its Telegram credentials and sessions are
server-scoped.
