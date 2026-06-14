# Message Immediate Recognition Design

## Goal

Add a first-version manual recognition loop for each group message in the Web execution desk.

## Chosen Approach

Use the existing rule-based text parser for V1 immediate recognition. Each message card gets an `立即识别` button. Clicking it calls a backend API, parses the current message text, writes or updates a `SignalCandidate` when the message looks like a strategy, and refreshes the current message list so the AI result block shows the new state.

## Navigation

Add a compact menu below the `交易执行台` title:

- `主界面`: shows the current trading execution desk.
- `AI识别提示词`: shows an editor for the message-recognition prompt. V1 stores the prompt in a project config file so backend recognition can use the same source later.
- `AI配置`: shows basic AI configuration placeholders and current V1 mode, making it clear that current immediate recognition uses the local rule parser.

## V1 Recognition Semantics

- Existing `SignalCandidate`: show `是策略` and strategy content.
- Text message with parser confidence above zero: create/update `SignalCandidate`, then show `是策略`.
- Text message with no parsed signal: show `非策略`.
- Image message: keep `待识别` for now, because OCR/LLM image recognition is a later step.
- Video message: show `非策略`, reason `视频消息默认跳过`.
- Recognition failures return a visible error and do not create candidates.

## Notes

This design intentionally separates UI/prompt configuration from actual LLM execution. The prompt page is useful now as the future contract for LLM recognition, while the button delivers an immediately testable local workflow.
