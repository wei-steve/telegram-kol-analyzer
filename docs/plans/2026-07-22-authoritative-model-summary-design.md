# Authoritative Model Summary Design

## Goal

Replace the confusing four-source “历史实验（非权威）” message panel with the two
models that actually determine the current recognition decision.

## Scope

- Render an authoritative-summary panel only for messages with a persisted
  `RecognitionDecision`.
- Show the authoritative MiMo analysis and DeepSeek auxiliary review, labelled
  from their actual decision roles and model names.
- Remove the retired GLM-OCR and MiMo text/image experiment rows from this
  message-detail surface.
- Keep recognition, management planning, and exchange execution unchanged.

## Data flow

The message query layer will serialize the persisted `RecognitionDecision` as
two display rows: the authoritative result and the auxiliary review. The
template will render only those rows. A persisted `MessageRecognition` remains
available for compatibility and diagnostics, but will not be presented as
“DeepSeek text,” since it may have been produced by MiMo.

## Verification

- Add a failing render test proving a MiMo-backed recognition is never labelled
  “DeepSeek text.”
- Assert the page shows MiMo and DeepSeek decision rows and omits the retired
  labels.
- Run the focused web-query and page-render tests.
