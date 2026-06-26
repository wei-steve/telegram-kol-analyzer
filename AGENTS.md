# Project Workflow

- Make code changes and run tests locally first.
- Push reviewed local commits to GitHub on `codex/deepcoin-auto-trading-v1`.
- Update production by pulling from GitHub on the server, reinstalling the editable package, and restarting `telegram-kol.service`.
- Prefer the existing helper after pushing:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\server_git_update.ps1
```

