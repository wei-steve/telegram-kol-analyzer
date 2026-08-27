param(
    [string]$Server = "root@43.167.220.225",
    [string]$KeyPath = "$HOME\.ssh\tecent.pem",
    [string]$Branch = "codex/deepcoin-auto-trading-v1",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedCommit,
    [Parameter(Mandatory = $true)]
    [ValidateSet("enabled", "disabled")]
    [string]$ExpectedAutoTradeState
)

$ErrorActionPreference = "Stop"

if ($Branch -notmatch '^[A-Za-z0-9._/-]+$') {
    throw "Branch contains unsupported characters."
}

$updaterPath = Join-Path $PSScriptRoot "..\deploy\telegram-kol-update"
$updaterSha = (Get-FileHash -Algorithm SHA256 $updaterPath).Hash.ToLowerInvariant()
$topologyContract = "dual-v1"
$cacheArtifactContract = "worker-cache-v1"
$monitorExpectationContract = "monitor-expectation-v1"
$bootstrapScript = @'
set -euo pipefail
expected_commit="$1"; branch="$2"; expected_sha="$3"; topology_contract="$4"; cache_artifact_contract="$5"; monitor_expectation_contract="$6"; expected_auto_trade_state="$7"
app_dir="/opt/telegram-kol-analyzer"
temporary="$(mktemp -d /run/telegram-kol-update.bootstrap.XXXXXX)"
chmod 0700 "$temporary"
trap 'rm -rf -- "$temporary"' EXIT
git -C "$app_dir" fetch origin "$branch"
[ "$(git -C "$app_dir" rev-parse FETCH_HEAD)" = "$expected_commit" ]
git -C "$app_dir" show "$expected_commit:deploy/telegram-kol-update" >"$temporary/updater"
chmod 0700 "$temporary/updater"
[ "$(sha256sum "$temporary/updater" | awk '{print $1}')" = "$expected_sha" ]
[ "$topology_contract" = "dual-v1" ]
[ "$cache_artifact_contract" = "worker-cache-v1" ]
[ "$monitor_expectation_contract" = "monitor-expectation-v1" ]
grep -Fq 'resolve_managed_topology()' "$temporary/updater"
grep -Fq 'install_worker_cache_artifacts' "$temporary/updater"
grep -Fq 'telegram-kol-worker-prepare-contract-cache' "$temporary/updater"
grep -Fq 'sync_monitor_expectations' "$temporary/updater"
grep -Fq 'install_monitor_service_artifact' "$temporary/updater"
grep -Fq 'telegram-kol-ingest.service' "$temporary/updater"
grep -Fq 'telegram-kol-worker.service' "$temporary/updater"
grep -Fq 'telegram-kol-web.service' "$temporary/updater"
EXPECTED_COMMIT="$expected_commit" EXPECTED_AUTO_TRADE_STATE="$expected_auto_trade_state" BRANCH="$branch" bash "$temporary/updater"
'@
$encodedBootstrap = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($bootstrapScript)
)
$remote = "printf '%s' '$encodedBootstrap' | base64 -d | bash -s -- " +
    "'$($ExpectedCommit.ToLowerInvariant())' '$Branch' '$updaterSha' '$topologyContract' '$cacheArtifactContract' '$monitorExpectationContract' '$ExpectedAutoTradeState'"
ssh -i $KeyPath $Server $remote
if ($LASTEXITCODE -ne 0) {
    throw "Server deployment failed with exit code $LASTEXITCODE."
}
