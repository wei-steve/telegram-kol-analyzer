param(
    [string]$Server = "root@43.167.220.225",
    [string]$KeyPath = "$HOME\.ssh\tecent.pem",
    [string]$Branch = "codex/deepcoin-auto-trading-v1",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedCommit,
    [Parameter(Mandatory = $true)]
    [ValidateSet('code', 'schema_compatible', 'execution_writer', 'live_promotion')]
    [string]$ChangeClass,
    [ValidatePattern('^$|^/[A-Za-z0-9._/-]+$')]
    [string]$ReviewedShadowEvidencePath = "",
    [ValidatePattern('^$|^/[A-Za-z0-9._/-]+$')]
    [string]$PreviousLiveSnapshotPath = "",
    [switch]$AuthorizeLivePromotion
)

$ErrorActionPreference = "Stop"

if ($Branch -notmatch '^[A-Za-z0-9._/-]+$') {
    throw "Branch contains unsupported characters."
}

$updaterPath = Join-Path $PSScriptRoot "..\deploy\telegram-kol-update"
$updaterSha = (Get-FileHash -Algorithm SHA256 $updaterPath).Hash.ToLowerInvariant()
$authorization = if ($AuthorizeLivePromotion) { "I_AUTHORIZE_LIVE_PROMOTION" } else { "" }
$bootstrapScript = @'
set -euo pipefail
expected_commit="$1"; branch="$2"; expected_sha="$3"; change_class="$4"
shadow_path="$5"; previous_snapshot="$6"; authorization="$7"
app_dir="/opt/telegram-kol-analyzer"
temporary="$(mktemp /run/telegram-kol-update.bootstrap.XXXXXX)"
trap 'rm -f "$temporary"' EXIT
git -C "$app_dir" fetch origin "$branch"
[ "$(git -C "$app_dir" rev-parse FETCH_HEAD)" = "$expected_commit" ]
git -C "$app_dir" show "$expected_commit:deploy/telegram-kol-update" >"$temporary"
[ "$(sha256sum "$temporary" | awk '{print $1}')" = "$expected_sha" ]
chmod 0755 "$temporary"
EXPECTED_COMMIT="$expected_commit" CHANGE_CLASS="$change_class" BRANCH="$branch" \
REVIEWED_SHADOW_EVIDENCE_PATH="$shadow_path" \
PREVIOUS_LIVE_SNAPSHOT_PATH="$previous_snapshot" \
LIVE_PROMOTION_AUTHORIZATION="$authorization" \
  "$temporary"
'@
$encodedBootstrap = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($bootstrapScript)
)
$remote = "printf '%s' '$encodedBootstrap' | base64 -d | bash -s -- " +
    "'$($ExpectedCommit.ToLowerInvariant())' '$Branch' '$updaterSha' '$ChangeClass' " +
    "'$ReviewedShadowEvidencePath' '$PreviousLiveSnapshotPath' '$authorization'"
ssh -i $KeyPath $Server $remote
if ($LASTEXITCODE -ne 0) {
    throw "Server deployment failed with exit code $LASTEXITCODE."
}
