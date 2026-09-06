# Media Integrity Recovery Design

## Context

Telegram message 9668 contained a complete BTC 66700 short strategy, but its
downloaded image was zero bytes. The message was stored with a non-null media
path, so the existing replay logic treated it as downloaded and never retried
it. Recognition consequently failed without an operational alert.

## Decision

Introduce one shared definition of usable image media: the path must exist,
be a regular non-empty file, and be decodable as an image. Use that definition
at download completion, cache reuse, replay selection, and recognition error
reporting.

Downloads will be written to a temporary target and promoted only after
validation. A failed or invalid transfer must not be persisted as a usable
media path. Replays will select all unusable media rows, not only rows whose
path is NULL, and will update the media row after a successful re-download.

## Safety

Media recovery only restores source evidence. Recovered historical messages
may be inspected or recognized through the existing side-channel experiment,
but they must not be passed to the live auto-trade executor. Existing live
message processing remains unchanged.

## Failure Visibility

The recognition-facing reason will distinguish absent, empty, and corrupt
files. The replay result will remain observable through the stored media path
and existing recognition status. A subsequent operational alert/status surface
can consume the same shared usability predicate without duplicating rules.

## Verification

Tests cover zero-byte and corrupt cached images, a failed new download,
selection of unusable media for replay, successful replacement and normal
non-empty cache reuse. Run focused Telegram-fetch and listener tests, then the
full local suite that does not require production credentials. On the server,
perform a read-only media-integrity scan and verify no historical recovery
causes an exchange request.
