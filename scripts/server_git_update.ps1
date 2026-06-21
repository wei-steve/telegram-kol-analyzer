param(
    [string]$Server = "root@43.167.220.225",
    [string]$KeyPath = "$HOME\.ssh\tecent.pem",
    [string]$Branch = "codex/deepcoin-auto-trading-v1"
)

$ErrorActionPreference = "Stop"

$remote = "BRANCH='$Branch' /usr/local/bin/telegram-kol-update"
ssh -i $KeyPath $Server $remote
