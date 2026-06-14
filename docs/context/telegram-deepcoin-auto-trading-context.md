# Telegram KOL Deepcoin Auto-Trading Context

Last updated: 2026-06-12

This document preserves the project context for future sessions. It intentionally does not store API keys, secret keys, passphrases, phone numbers, bot tokens, or chat IDs.

## User Goal

Build a local-first automation system that monitors selected Telegram KOL groups, uses AI to classify and extract trading signals from text and images, sends entry/exit/update alerts, and optionally performs fully automated Deepcoin futures trading.

The user wants a runtime switch:

- Auto mode on: if AI extraction and risk checks pass, automatically enter, exit, adjust stop loss, and manage take profit orders.
- Auto mode off: do not trade automatically, but still send alerts and record decisions.

Notifications should be sent to a user-configured Telegram Bot first. Feishu can be added later. Notification credentials should be supplied by the user in local config, not stored in the repository.

## Telegram Requirements

- Source messages come from multiple Telegram groups containing Bitcoin/futures KOLs.
- Messages may be text, image, or video links.
- The system must identify which KOL or analyst produced the signal. Some groups have one KOL; some groups contain multiple analysts, e.g. `大漂亮/#Nick`, `大镖客/币姐`, `大镖客/Andy`.
- The system must filter noise: marketing, chat, educational text, recaps, performance bragging, irrelevant links, and non-actionable opinions.
- The system must detect:
  - Entry signals
  - Market entry
  - Limit/range entry
  - Temporary entry
  - Temporary exit
  - Full close
  - Partial close / take profit
  - Stop loss updates
  - Take profit updates
  - Cancelled/invalidated setups
  - Strategy changes from later KOL messages

## Current Telegram Implementation

The existing project uses Telethon with a Telegram user account, not the Telegram Bot API, for reading target groups.

Relevant existing modules:

- `src/telegram_kol_research/telegram_client.py`
- `src/telegram_kol_research/telegram_live_listener.py`
- `src/telegram_kol_research/raw_ingest.py`
- `src/telegram_kol_research/strategy_alerts.py`
- `src/telegram_kol_research/candidates.py`
- `src/telegram_kol_research/trade_merge.py`
- `src/telegram_kol_research/web_app.py`

Existing auth/config behavior:

- Telegram credentials are loaded from env files or shell environment.
- Session file is local under `data/telegram.session`.
- Target groups are configured in `config/groups.yaml`.

The current project already supports:

- Discovering archived target Telegram dialogs.
- Syncing history.
- Realtime listener.
- Web workbench.
- Storing raw messages and media metadata.
- Basic text candidate parsing.
- Basic AI strategy alert forwarding.

Important current gap:

- Historical sync currently records media metadata but does not download image files by default.
- Existing parser misses many Chinese strategy formats.
- Existing alert flow skips empty-text media and does not yet support image strategy recognition.
- There is no real trade execution layer yet.

## Recent Telegram Data Observed

On 2026-06-12, a history sync was run with `--mode full --message-limit 1000`.

Result:

- 13 archived target groups discovered.
- 3597 raw messages inserted.
- 119 signal candidates inserted by current parser.
- 22 trade ideas inserted by current merge logic.

Configured groups: 18.

Matched groups:

- `ROSE会员群-11分组`
- `币圈所长会员群-11分组`
- `大漂亮社区 11分组`
- `比特币飞扬 11分组`
- `比特币军长-11分组`
- `峰哥高级会员群-11分组`
- `大镖客 11分组`
- `米哥会员群-11分组`
- `比特币陈哥会员群-11分组`
- `三马哥会员群-11分组`
- `舒琴会员群-11分组`
- `il Capo Of Crypto加密首席`
- `凉兮 凉兮 11分组`

Configured but not matched:

- `书'shu-crypto 11分组`
- `二姐精准策略群ATM11分组`
- `提阿非罗 切塔 11分组`
- `米娅 vip 会员群 11分组`
- `赛有财策略群--11分组`

Recent 7-day local database stats from 2026-06-05 to 2026-06-12:

- 1028 raw messages.
- 542 media assets.
- 0 recent media assets had local downloaded paths at the time checked.
- Existing rule parser mainly caught English-style signals such as `#BTC LONG`, but missed many Chinese signals like `建仓/止损/止盈/现价/平仓`.

Examples of Chinese strategy formats observed:

- `比特币现货，62800-60000做多，均价61400，64200-65400-66600止盈，止损59500`
- `Btc 方向：空 建仓：63600-64700 止损：65100 止盈：62900-62200-61500`
- `比特币 62900-62650 做多 ... 止损 ... 止盈 ...`
- `BTC 做空 ... 市价上车 ... 再挂 ... 第一止盈 ... 第二止盈 ... 全部止盈 ... 止损 ...`
- `现价开一层空单`
- `多单止盈掉`
- `剩余仓位全部止盈出局`

## Trading Rules Confirmed

Default risk:

- Fixed loss sizing: default maximum loss is `100 USDT` per trade.
- Different KOLs may override this value.

Allowed symbols:

- Default tradable symbols: BTC and ETH futures only.
- User can extend symbol whitelist later.
- Some KOL-mentioned symbols may not exist on Deepcoin, so symbol whitelist and exchange instrument availability are mandatory checks.

Entry logic:

- If KOL gives an entry range, e.g. `62650-62900 做多`:
  - If current price is inside the range: enter market.
  - If current price is outside the range: place 50% of size at one range edge and 50% at the range average. The exact edge selection still needs final confirmation.
- If KOL says `现价开多/开空`:
  - Find a reference price.
  - Enter market only if current price is within a configured percentage deviation from reference price. Suggested default is 0.15%, but not yet confirmed.

Stop loss:

- If there is no explicit stop loss, do not auto-trade.
- Send alert and move to manual review/confirmation.

Take profit:

- If there are multiple TP levels, split position evenly across TP levels.
- Later KOL messages can modify the strategy and should update existing TP/SL/exit plan.

Position attribution:

- Multiple KOLs may hold same symbol and same direction at different prices.
- Must precisely attribute each position to the KOL signal that created it.
- Desired binding key:
  - `kol_id`
  - `chat_id`
  - `message_id`
  - `symbol`
  - `side`
  - Deepcoin `posId`

Manual review required:

- Image-recognized signals.
- Missing stop loss.
- Temporary exit but no matching position found.
- Reverse signal.
- Same KOL already has same-symbol same-direction active position.
- AI confidence below threshold.

KOL control:

- Each group and each analyst should be configurable.
- Some groups/KOLs are notify-only because the user does not fully trust them.
- Default recommended behavior: all KOLs start as `notify_only`; user explicitly enables `auto_trade` per KOL/analyst.

## AI Requirements

AI must be configurable per task in a local config file.

Expected AI tasks:

- Cheap text filter/classifier.
- Vision model for image strategy recognition.
- Strategy structure extraction model.
- High-risk second-pass review model.
- Summary/backtest/analysis model.

The system should track token usage and cost per provider/model/task/message when available.

Context strategy:

- Some tasks are one-shot per message.
- Some tasks should use context, such as recent messages from the same KOL or thread when interpreting updates like `平仓`, `止损放...`, `剩余半仓`, or `这个单子`.
- DeepSeek or other cached-context providers may be useful for cost savings.

## Runtime Requirements

Confirmed runtime mode:

- Local background service.
- Telegram realtime new-message listener.
- Periodic reconcile/backfill every 5 minutes to reduce missed messages.
- Web workbench for status, review, config, and logs.

## Deepcoin Requirements

Trading venue: `www.deepcoin.com`.

Important user constraint:

- API trading reportedly does not receive the desired fee rebate.
- Browser trading reportedly receives 90% trading fee rebate.
- Therefore, API should be used for read-only account/order/position verification, while browser automation should be the preferred live order path if auto-trading is enabled.

Deepcoin API docs:

- Authentication docs: `https://www.deepcoin.com/docs/zh/authentication`
- Account balance: `https://www.deepcoin.com/docs/zh/DeepCoinAccount/getAccountBalance`
- Unified balance: `https://www.deepcoin.com/docs/zh/DeepCoinAccount/getAllAccountBalances`
- Positions: `https://www.deepcoin.com/docs/zh/DeepCoinAccount/accountPositions`
- Trade: `https://www.deepcoin.com/docs/zh/DeepCoinTrade/order`

Deepcoin API auth details from docs:

- Base URL: `https://api.deepcoin.com`
- Private REST headers:
  - `DC-ACCESS-KEY`
  - `DC-ACCESS-SIGN`
  - `DC-ACCESS-TIMESTAMP`
  - `DC-ACCESS-PASSPHRASE`
- Signature string: `timestamp + method + requestPath + body`
- Signature algorithm: HMAC-SHA256 with SecretKey, then Base64.
- GET query params are part of `requestPath`, not body.

User provided a read-only API key, secret, and candidate passphrase in chat. Do not store or echo them in code/docs. It was verified once that the candidate passphrase works for read-only API calls.

Deepcoin account snapshot observed on 2026-06-12 via read-only API:

- Total equity: approximately `51247.48043262 USD`.
- Total available USDT: approximately `49669.46 USDT`.
- Funding account:
  - `94.7 USDT` balance/equity.
- USDT futures account:
  - Balance approximately `50568.03 USDT`.
  - Available approximately `49574.76 USDT`.
  - Frozen approximately `992.77 USDT`.
  - Unrealized PnL approximately `-0.022 USDT`.
- Rebate account:
  - Equity approximately `584.77243262 USDT`.
  - Available `0`.
  - Frozen approximately `584.77243262`.
- Spot account:
  - `0 USDT`.
- Coin-margined futures, bonus, event, copy trading, and robot accounts had no balance details in the snapshot.

Position snapshot observed:

- One BTC futures position was returned by API:
  - `BTC-USDT-SWAP`
  - `long`
  - `cross`
  - `split`
  - `pos` = `1`
  - `avgPx` approximately `62976`
  - leverage `125x`
  - unrealized PnL around `-0.0221 USDT`
  - TP/SL empty in API response.
- Web page later showed `持仓(0)` while API had shown a small position. The execution design must treat API as final source of truth and use browser state as a secondary visual/execution surface.

## Deepcoin Browser Structure Observed

The user logged in manually and opened:

- `https://www.deepcoin.com/swap/zh/BTCUSDT`

Observed page title:

- `BTCUSDT - Deepcoin` with live price in title.

Observed trade panel:

- Current page pair: `BTCUSDT 永续`.
- Current visible mode: `全仓 合仓`.
- Visible leverage: `125/125x`.
- Trade tabs:
  - `开仓`
  - `平仓`
- Order type tabs:
  - `市价`
  - `限价`
  - `条件`
- Input:
  - `合约价值` textbox, unit `USDT`.
- Checkbox:
  - `止盈止损`.
- Buttons:
  - `买入/开多`
  - `卖出/开空`
- Balance panel:
  - visible USDT account asset around `49,575.30`.
- Position/order tabs:
  - `持仓(0)`
  - `当前委托(14)`
  - `跟单(0)`
  - `策略(0)`
  - `历史委托`
  - `历史仓位`

Observed mode settings dialog:

- Opens from the `全仓 合仓` selector.
- Dialog title: `交易模式设置`.
- Margin mode options:
  - `全仓`
  - `逐仓`
- Position mode options:
  - `合仓`
  - `分仓`
- Confirm button:
  - `确定`.

Browser conclusion:

- Deepcoin web supports both full/cross vs isolated and merge vs split.
- For KOL attribution, production execution should prefer `分仓`.
- Browser selectors include some stable-ish text and container names, but hashed CSS class suffixes may change. Automation should combine:
  - Current URL/instrument check.
  - Visible text checks.
  - Scoped panel search.
  - Pre-submit form readback.
  - Post-submit API verification.

Suggested Deepcoin architecture:

- `DeepcoinReadOnlyAPI`
  - balances
  - positions
  - open orders
  - fills/order history
  - post-submit verification
  - `posId` mapping
- `DeepcoinBrowserExecutor`
  - open/switch instrument page
  - verify logged-in account
  - verify or set margin mode
  - verify or set position mode
  - choose market/limit order
  - fill value/quantity
  - configure TP/SL when safe
  - pre-submit readback
  - submit only when auto mode is enabled and all checks pass
  - never guess after UI/API mismatch

## Safety Constraints

- Do not store secrets in repository.
- Use `.env` or local config ignored by git for:
  - Deepcoin API key
  - Deepcoin secret key
  - Deepcoin passphrase
  - Telegram bot token
  - Telegram chat ID
  - AI provider keys
- Browser execution must fail closed:
  - Login expired.
  - CAPTCHA.
  - Page structure changed.
  - Selector ambiguous.
  - API verification failed.
  - Unexpected existing position/order.
  - Auto mode off.
  - AI confidence too low.

## Existing Private Project

The user has an older private GitHub project:

- `wei-steve/DC-trading`

Purpose:

- Desktop/browser Deepcoin auto-trading.
- Built around 4 months before 2026-06-12.
- May contain useful selectors and automation patterns.

Status:

- Anonymous `git clone` failed with GitHub authentication error.
- Public search did not find the repository, so it is likely private.
- Need user to grant access or provide key files.

Recommended files to inspect if access is granted:

- README / setup docs.
- Browser automation code.
- Selector definitions.
- Order open/close functions.
- Deepcoin login/session handling.
- Risk checks.
- Any config/secrets examples, with secrets removed.

## Next Design Work

Design document should cover:

- Config schema:
  - Telegram groups.
  - KOL/analyst profiles.
  - auto mode switch.
  - per-KOL risk.
  - symbol whitelist.
  - AI model routing.
  - Deepcoin execution preferences.
  - notification settings.
- Database schema additions:
  - AI decisions.
  - extracted strategies.
  - manual review queue.
  - virtual/live trade ledger.
  - Deepcoin execution records.
  - token usage.
  - risk events.
- Message pipeline:
  - Telegram event.
  - media download.
  - OCR/vision.
  - cheap filter.
  - structured extraction.
  - context-aware update matching.
  - risk checks.
  - alert.
  - optional execution.
  - API verification.
- Deepcoin browser/API execution workflow.
- Web workbench UI for:
  - auto mode switch.
  - KOL config.
  - review queue.
  - live decisions.
  - current positions.
  - execution logs.
  - token/cost usage.
- Test strategy:
  - replay historical Telegram messages.
  - AI fixture tests.
  - dry-run execution tests.
  - Deepcoin API mock tests.
  - browser selector smoke tests.
  - fail-closed tests.

