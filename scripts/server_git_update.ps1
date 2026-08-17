param(
    [string]$Server = "root@43.167.220.225",
    [string]$KeyPath = "$HOME\.ssh\tecent.pem",
    [string]$Branch = "codex/deepcoin-auto-trading-v1",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedCommit
)

$ErrorActionPreference = "Stop"

if ($Branch -notmatch '^[A-Za-z0-9._/-]+$') {
    throw "Branch contains unsupported characters."
}

$updaterPath = Join-Path $PSScriptRoot "..\deploy\telegram-kol-update"
$updaterSha = (Get-FileHash -Algorithm SHA256 $updaterPath).Hash.ToLowerInvariant()
$bootstrapScript = @'
set -euo pipefail
expected_commit="$1"; branch="$2"; expected_sha="$3"
app_dir="/opt/telegram-kol-analyzer"
temporary="$(mktemp -d /run/telegram-kol-update.bootstrap.XXXXXX)"
chmod 0700 "$temporary"
trap 'rm -rf -- "$temporary"' EXIT
git -C "$app_dir" fetch origin "$branch"
[ "$(git -C "$app_dir" rev-parse FETCH_HEAD)" = "$expected_commit" ]
git -C "$app_dir" show "$expected_commit:deploy/telegram-kol-update" >"$temporary/updater"
chmod 0700 "$temporary/updater"
[ "$(sha256sum "$temporary/updater" | awk '{print $1}')" = "$expected_sha" ]
EXPECTED_COMMIT="$expected_commit" BRANCH="$branch" bash "$temporary/updater"
'@
$encodedBootstrap = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($bootstrapScript)
)
$remote = "printf '%s' '$encodedBootstrap' | base64 -d | bash -s -- " +
    "'$($ExpectedCommit.ToLowerInvariant())' '$Branch' '$updaterSha'"
ssh -i $KeyPath $Server $remote
if ($LASTEXITCODE -ne 0) {
    throw "Server deployment failed with exit code $LASTEXITCODE."
}
