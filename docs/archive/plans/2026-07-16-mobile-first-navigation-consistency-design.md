# Mobile-First Navigation Consistency Design

## Goal

Make the Web workbench reliable and comfortable on phone browsers while preserving the existing desktop experience and all live-trading safety boundaries.

## Problems

The current page has two independent navigation state machines. Workbench navigation controls `首页 / 持仓 / 策略 / 消息 / 批次 / 更多`, while dashboard navigation separately controls `交易设置 / AI 配置 / 交易持仓` and related panels. Opening a dashboard panel does not deactivate the current workbench panel, so the home event feed and trading settings can occupy the same grid cell and overlap.

The lazy-loading optimization also treats `策略` and `消息` as fully separate destinations. That is correct on phone, but desktop still renders a three-column strategy workspace. Entering `策略` loads only the middle column and leaves the visible message column at its loading placeholder.

The phone header and bottom navigation expose too many simultaneous actions. Six bottom destinations make targets cramped, and the desktop settings dropdown covers home metrics on narrow screens.

## Design

### One primary surface at a time

Use one navigation coordinator for both workbench destinations and dashboard/settings panels. Activating a settings panel deactivates every workbench panel. Returning from settings restores the workbench destination that opened it. `交易持仓` maps directly to the normal `持仓` destination instead of behaving like a second competing route.

The coordinator owns:

- the active workbench view stored on `data-trader-dashboard`;
- active classes and `aria-current` on desktop and mobile navigation buttons;
- active dashboard/settings panel;
- the return destination for settings pages;
- destination-level lazy loading.

### Phone-first navigation

The phone bottom bar contains five destinations: `首页 / 持仓 / 策略 / 消息 / 更多`. `批次` moves under `更多`; it remains a first-class destination in desktop navigation and remains reachable on phone from the More screen.

At phone widths, the settings dropdown becomes a bottom sheet with full-width 44px actions. The header hides redundant desktop links because `更多` already exposes those destinations. Settings content occupies the full primary content area and uses a sticky save/action row where present.

### Strategy and message loading

Phone behavior remains destination-based: `策略` loads strategy content and `消息` loads the message timeline.

At widths of 761px and above, the strategy workspace visibly includes the message column. After the strategy panel becomes usable, load the selected group's message detail as a guarded companion request. The companion request must not delay group-selection feedback and must check the request ID before writing to the DOM so a stale response cannot overwrite a newer group.

### Safety boundaries

This change is presentation and read-path only. It does not change recognition, lifecycle mutation, order construction, Deepcoin calls, trading settings semantics, confirmation requirements, or live-position actions.

## Error Handling

- Settings and workbench panels must never both remain active.
- A failed desktop companion-message request leaves the strategy panel usable and shows the existing retryable message loading state when the user opens `消息`.
- Stale companion responses are ignored using the existing `groupSwitchRequestId` guard.
- Phone navigation retains at least 44px touch targets and safe-area padding.

## Verification

- Static regression tests assert one coordinated navigation path, five phone destinations, phone bottom-sheet styling, and the guarded desktop companion loader.
- Existing Web render and asset tests remain green.
- JavaScript syntax and whitespace checks pass.
- Browser verification covers 390x844 phone and desktop widths for home, settings, strategy, message, return navigation, and group switching.
- Production verification confirms served asset markers and `telegram-kol.service` health without submitting any trade action.

