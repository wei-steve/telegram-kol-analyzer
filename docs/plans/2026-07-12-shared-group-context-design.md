# Shared Group Context Design

## Goal

Make group selection an obvious shared context for the strategy and message destinations on mobile and desktop. Selecting a group once must update both destinations, while the home event feed remains global.

## Confirmed Decisions

- `策略` and `消息` share one current group.
- `首页` continues to show events from all groups.
- `持仓` remains global and may keep its existing attribution/group filters.
- `更多` is not group-scoped.
- The message-only select introduced in the previous fix is replaced by a shared context control.

## Navigation Model

The shared group context appears immediately below the application header whenever the active destination is `策略` or `消息`:

```text
当前群组
比特币陈哥会员群-11分组                    切换 ›
```

The context bar is sticky below the system header. It displays the selected group consistently on both destinations. Switching destinations does not reset the group.

The home destination does not show the bar and does not filter its timeline by the selected group.

## Mobile Group Picker

Tapping the context bar opens a bottom sheet containing:

- A title and persistent close control.
- Search by group title, KOL label, or configured alias.
- The three most recently used groups, when available.
- All groups ordered by latest message activity.
- A selected-state checkmark.
- Compact group workload metadata: holding and pending strategy counts.

The sheet owns its scroll area so the title, close control, and search field remain available. Rows have at least a 48px touch target. Long group names truncate in the context bar but display fully inside the picker.

## Desktop Group Picker

Desktop uses the same context bar and state. Activating it opens a compact anchored popover rather than a bottom sheet. Search, recent groups, ordering, selection, and workload metadata are identical to mobile.

The existing group list may remain as an auxiliary browse view during migration, but the context control is the canonical selection surface. Both surfaces must call the same selection controller and cannot maintain independent selected values.

## State and Data Flow

The server continues to render group rows with `chat_id`, display title, last activity, holding count, and pending count. The browser owns one selected group ID and persists it in `localStorage`.

On first load:

1. Read the persisted group ID.
2. Use it if it still exists in the rendered group rows.
3. Otherwise use the server-selected/default group.
4. Synchronize the context bar, picker row, legacy group row, strategy panel, and message panel.

On selection:

1. Show a pending state in the context bar.
2. Fetch strategy and message fragments for the target group.
3. Commit the UI state only after the active destination succeeds.
4. Update both destinations' selected group identity and persist the group ID.
5. Close the picker and announce the new group through an accessible status region.

The current destination determines which visible content refreshes immediately. The other destination uses the same selected group and refreshes when opened.

## Failure and Empty States

- If switching fails, retain the previous group and content.
- Show `切换失败，点击重试` in the context bar.
- Do not persist the failed group selection.
- If no groups exist, disable the context bar and explain that no Telegram groups are configured or synchronized.
- If a persisted group no longer exists, fall back silently to the first current server group.

## Accessibility

- The context bar is a button with the current group in its accessible name.
- The mobile sheet uses a dialog with a labelled title and focus return.
- The desktop picker supports keyboard navigation and Escape to close.
- Selection is expressed with text/checkmark in addition to color.
- Search and result counts are announced without forcing focus changes.

## Validation

Automated coverage must verify:

- The context bar appears for strategy/message context and exposes all groups.
- Selecting a group updates shared selected state.
- Strategy and message requests use the same `chat_id`.
- Home remains unfiltered.
- Local persistence restores a valid group and rejects a removed group.
- Search, recent ordering, empty results, switch failure, dialog close, and retry behavior.
- Mobile sheet and desktop popover responsive CSS contracts.

Real server verification checks configured production groups, mobile touch behavior, switching between strategy and message destinations, refresh persistence, and service health.

