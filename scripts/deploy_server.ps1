param(
    [string]$Server = "root@43.167.220.225",
    [string]$KeyPath = "$HOME\.ssh\tecent.pem",
    [string]$RemoteDir = "/opt/telegram-kol-analyzer",
    [switch]$SyncData,
    [switch]$IncludeMedia,
    [switch]$RestartOnly
)

$ErrorActionPreference = "Stop"

function Invoke-Remote {
    param([string]$Command)
    ssh -i $KeyPath $Server $Command
}

if ($RestartOnly) {
    Invoke-Remote "systemctl restart telegram-kol.service && systemctl --no-pager --full status telegram-kol.service | head -n 15"
    exit 0
}

$archive = Join-Path $env:TEMP "telegram-kol-analyzer-deploy.tar.gz"
Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue

$excludes = @(
    "--exclude=.git",
    "--exclude=.venv",
    "--exclude=.pytest_cache",
    "--exclude=.tmp",
    "--exclude=*.log"
)

if (-not $SyncData) {
    $excludes += "--exclude=data"
} else {
    $excludes += "--exclude=data/*.lock"
    if (-not $IncludeMedia) {
        $excludes += "--exclude=data/media"
    }
}

tar @excludes -czf $archive .
scp -i $KeyPath $archive "${Server}:/tmp/telegram-kol-analyzer-deploy.tar.gz"

$remote = @"
set -e
mkdir -p "$RemoteDir"
tar -xzf /tmp/telegram-kol-analyzer-deploy.tar.gz -C "$RemoteDir"
cd "$RemoteDir"
if [ ! -x .venv/bin/python ]; then
  python3.12 -m venv .venv
fi
. .venv/bin/activate
python -m pip install -e .
systemctl restart telegram-kol.service
systemctl --no-pager --full status telegram-kol.service | head -n 18
"@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
Invoke-Remote "echo $encoded | base64 -d | bash"
