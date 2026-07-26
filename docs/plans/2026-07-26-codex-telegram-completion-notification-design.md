# Codex Telegram Completion Notification Design

## Goal

Send a Telegram message to the project owner's existing notification chat after
every completed Codex task in this repository. Keep the Bot Token out of the
repository, command output, documentation, and logs.

## Selected Approach

Use a local helper script that retrieves the Bot Token from macOS Keychain and
calls Telegram's Bot API. The destination chat is the existing server-configured
notification chat, `8129644952`.

The project-level `AGENTS.md` instructs every Codex session to call the helper
before its final response. If Telegram delivery fails, the session falls back to
the existing macOS notification and reports the failure in its final response.

## Components

- A repository helper script sends a concise completion message through the
  Telegram Bot API.
- macOS Keychain stores the Bot Token under a project-specific service/account
  name.
- `AGENTS.md` defines the mandatory completion-notification command and fallback.
- The message contains a stable project label and a short task-completion
  summary. It must not contain credentials or other sensitive values.

## Data Flow

1. A Codex task reaches successful completion.
2. Codex invokes the helper with a short, non-sensitive summary.
3. The helper reads the Bot Token from macOS Keychain.
4. The helper sends the message to Telegram chat `8129644952`.
5. A successful Telegram API response produces a zero exit status.
6. On failure, Codex sends the existing macOS notification and mentions the
   Telegram delivery failure in the final response.

## Security and Error Handling

- Never accept the Bot Token as a command-line argument.
- Never print the Bot Token or Telegram request URL containing it.
- Keep the token out of tracked and untracked project files.
- Use bounded network timeouts and fail with a concise, credential-free error.
- Validate Telegram's JSON response rather than treating HTTP transport success
  alone as delivery success.

## Verification

- Static checks confirm the helper contains no embedded credential.
- A missing-key test confirms a nonzero exit without leaking secrets.
- A live test sends one clearly labeled setup message to the configured chat.
- The project instruction is reviewed to confirm Telegram is primary and macOS
  notification is the fallback.
