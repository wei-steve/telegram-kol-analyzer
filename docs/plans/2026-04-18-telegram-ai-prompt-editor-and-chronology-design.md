# Telegram AI Prompt Editor and Chronology Design

**Date:** 2026-04-18

## Goal

Refine the simplified Telegram AI panel so each group has its own editable default prompt, the prompt takes effect on the next question immediately, the chat UI becomes visually cleaner, and AI grounding context is ordered chronologically to improve answer quality.

## Approved Direction

The user approved the following product behavior:

- Each Telegram group has its own default prompt
- The default prompt editor lives at the top of the right-side AI panel
- Editing the prompt should affect the next AI question immediately
- The chat UI should reduce noisy `YOU` / `AI` labels
- AI context should be provided in a more suitable chronological order so recent discussion evolution is clearer
- Image-input capability errors should be shown as cleaner user-facing guidance

## Product Design

### 1. Per-Group Default Prompt Editor

The top of the AI panel should contain a dedicated editor for the current group's default prompt.

The editor should:

- Load independently for each `chat_id`
- Save locally for now
- Show a small hint such as:
  - `仅影响当前群，下次提问立即生效`

Recommended storage key:

- `telegram-workbench:prompt:<chatId>`

Recommended default prompt content:

- Summarize the current group's recent messages as a trading-research assistant.
- Focus on the latest changes in sentiment, actionable signals, disagreements, and message-backed evidence.
- Prefer recent developments over older repeated claims.

### 2. Prompt Application Model

Prompt layering should be:

1. Fixed system instruction
2. Current group's editable default prompt
3. Chronologically ordered source context
4. Current user question

This keeps the user-editable prompt persistent without forcing them to restate their analysis preferences in every turn.

### 3. Cleaner Chat UI

The history area should stop emphasizing `YOU` / `AI` as repeated loud labels.

Instead:

- User questions should render as a compact question card
- Assistant answers should render as a primary response card
- Role indicators, if kept at all, should be subtle and secondary
- Source references should stay grouped under each assistant answer

### 4. Chronology-Aware Context

The browser can still display newest-first, but the AI context should be sent oldest-to-newest within the selected recent slice.

Example:

- UI list: latest message at top
- AI context: oldest message in selected slice first, newest last

This improves narrative continuity and reduces the chance that the model over-anchors on stale earlier discussion when the real meaning lies in later changes.

Also add an explicit instruction telling the model:

- messages are ordered chronologically
- later entries are newer and should be weighted more heavily for trend change and latest state

### 5. Error Handling for Image Capability Mismatch

If the proxy/model returns an image-input support error, the AI panel should show a cleaner Chinese explanation that makes the fallback clear.

Preferred wording:

- `当前模型不支持直接图片理解，本次分析会优先基于文字消息与 OCR 内容。`

This should feel like guidance rather than a raw backend error.

## Testing Strategy

Add or update tests for:

- per-group prompt editor rendering in the AI panel
- prompt value persistence behavior in frontend storage logic where practical
- chat API including user-editable default prompt in the payload pipeline
- chronology-aware context ordering (oldest to newest)
- cleaner image-support error wording

## Recommendation

Implement in one slice:

1. Add prompt editor UI and client-side storage
2. Include prompt text in `/api/chat` request
3. Extend backend payload builder to include editable prompt
4. Reverse scoped messages into chronological order before prompt assembly
5. Polish chat role labels and image-error message wording
