param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("plan", "push", "stage", "activate")]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$ActionManifest,
    [string]$Server = "root@43.167.220.225",
    [string]$KeyPath = "$HOME\.ssh\tecent.pem",
    [string]$Branch = "codex/deepcoin-auto-trading-v1",
    [string]$ExpectedCommit = "",
    [string]$RollbackCommit = "",
    [string]$ActivationAuthorization = "",
    [string]$ActivationAuthorizationConsumed = "",
    [ValidateSet("immutable", "stopped_legacy")]
    [string]$ActivationSourceMode = "immutable",
    [ValidateSet("0", "1")]
    [string]$ActivationDryRun = "0",
    [string]$ActivationControllerCommit = "",
    [string]$SourceRepo = "/opt/telegram-kol-analyzer",
    [string]$ReleaseRoot = "/opt/telegram-kol-releases",
    [string]$ServiceDropinRoot = "/etc/systemd/system",
    [string]$DatabasePath = "/opt/telegram-kol-analyzer/data/research.db"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonCandidates = @(
    (Join-Path $root ".venv\Scripts\python.exe"),
    (Join-Path $root ".venv/bin/python")
)
$plannerPython = $pythonCandidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $plannerPython) {
    throw "Planner Python is unavailable."
}
if (-not (Test-Path $ActionManifest -PathType Leaf)) {
    throw "ActionManifest is unavailable."
}

$planJson = & $plannerPython -m telegram_kol_research.deployment_action_plan `
    --manifest $ActionManifest --format json
if ($LASTEXITCODE -ne 0) {
    throw "Action manifest validation failed."
}
if ($Action -eq "plan") {
    $planJson
    exit 0
}
$plan = $planJson | ConvertFrom-Json
if ($plan.action -ne $Action) {
    throw "Action manifest does not match requested action."
}
$perRoleRollback = $null -ne $plan.rollback_releases
if ($ExpectedCommit -notmatch '^[0-9a-fA-F]{40}$') {
    throw "ExpectedCommit must be a full 40-character hexadecimal commit."
}
$ExpectedCommit = $ExpectedCommit.ToLowerInvariant()
if ($Branch -notmatch '^[A-Za-z0-9._/-]+$' -or $Branch.Contains("..")) {
    throw "Branch contains unsupported characters."
}

if ($Action -eq "push") {
    $dirty = & git -C $root status --porcelain --untracked-files=normal
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw "Push requires a clean worktree."
    }
    $head = (& git -C $root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -ne $ExpectedCommit) {
        throw "Push requires ExpectedCommit to equal the checked-out HEAD."
    }
    $remoteLine = & git -C $root ls-remote origin "refs/heads/$Branch"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the remote branch identity."
    }
    $remoteCommit = if ($remoteLine) { ($remoteLine -split '\s+')[0] } else { "" }
    if ($remoteCommit) {
        & git -C $root merge-base --is-ancestor $remoteCommit $ExpectedCommit
        if ($LASTEXITCODE -ne 0) {
            throw "Push would not be a fast-forward."
        }
    }
    & git -C $root push origin "${ExpectedCommit}:refs/heads/$Branch"
    if ($LASTEXITCODE -ne 0) {
        throw "Git push failed."
    }
    $pushedLine = & git -C $root ls-remote origin "refs/heads/$Branch"
    $pushedCommit = if ($pushedLine) { ($pushedLine -split '\s+')[0] } else { "" }
    if ($LASTEXITCODE -ne 0 -or $pushedCommit -ne $ExpectedCommit) {
        throw "Remote branch identity verification failed."
    }
    [pscustomobject]@{
        action = "push"
        commit = $ExpectedCommit
        status = "complete"
    } | ConvertTo-Json -Compress
    exit 0
}

if (-not (Test-Path $KeyPath -PathType Leaf)) {
    throw "SSH private key is not readable: $KeyPath"
}
function Test-SshDestination([string]$Value) {
    return $Value -match '^([A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$' -and
        -not $Value.Contains("..") -and -not $Value.Contains(".-") -and
        -not $Value.EndsWith("-.")
}
if (-not (Test-SshDestination $Server)) {
    throw "Invalid Server."
}
function Test-RemotePath([string]$Value) {
    return $Value -match '^/[A-Za-z0-9._/-]+$' -and
        -not $Value.Contains("..") -and -not $Value.Contains("//")
}
foreach ($remotePath in @($SourceRepo, $ReleaseRoot, $ServiceDropinRoot, $DatabasePath)) {
    if (-not (Test-RemotePath $remotePath)) {
        throw "Unsafe remote path."
    }
}
if ($Action -eq "activate") {
    if ($ActivationSourceMode -eq "immutable") {
        if ($perRoleRollback) {
            if ($RollbackCommit) {
                throw "RollbackCommit conflicts with rollback_releases."
            }
            if ($ActivationControllerCommit -notmatch '^[0-9a-f]{40}$') {
                throw "ActivationControllerCommit must be a full lowercase commit."
            }
        }
        elseif ($RollbackCommit -notmatch '^[0-9a-f]{40}$') {
            throw "RollbackCommit must be a full lowercase commit."
        }
    }
    if (-not (Test-RemotePath $ActivationAuthorization) -or
        -not (Test-RemotePath $ActivationAuthorizationConsumed)) {
        throw "Activation authorization paths are invalid."
    }
}

$manifestBytes = [IO.File]::ReadAllBytes((Resolve-Path $ActionManifest).Path)
if ($manifestBytes.Length -gt 65536) {
    throw "ActionManifest is too large."
}
$manifestBase64 = [Convert]::ToBase64String($manifestBytes)
$manifestSha = (Get-FileHash -Algorithm SHA256 $ActionManifest).Hash.ToLowerInvariant()
$bundleBase64 = "-"
$bundleSha = "-"
$bundlePath = $null
try {
    if ($Action -eq "stage" -or ($Action -eq "activate" -and $perRoleRollback)) {
        $bundlePath = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
        $bundleFiles = if ($Action -eq "stage") {
            @(
                "deploy/telegram-kol-stage",
                "src/telegram_kol_research/__init__.py",
                "src/telegram_kol_research/deployment_action_plan.py"
            )
        }
        else {
            @(
                "src/telegram_kol_research/__init__.py",
                "src/telegram_kol_research/deployment_activation_quiescence_check.py",
                "src/telegram_kol_research/deployment_active_write_check.py",
                "src/telegram_kol_research/deployment_action_plan.py",
                "src/telegram_kol_research/entry_revision_exchange_authority_contract.py",
                "src/telegram_kol_research/runtime_deployment_identity.py",
                "src/telegram_kol_research/scoped_release_activation.py"
            )
        }
        $bundleCommit = if ($Action -eq "activate") {
            $ActivationControllerCommit
        }
        else {
            $ExpectedCommit
        }
        & git -C $root archive --format=tar "--output=$bundlePath" $bundleCommit @bundleFiles
        if ($LASTEXITCODE -ne 0) {
            throw "Could not build the exact-commit control bundle."
        }
        $bundleBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($bundlePath))
        $bundleSha = (Get-FileHash -Algorithm SHA256 $bundlePath).Hash.ToLowerInvariant()
    }

    $remoteScript = @'
set -euo pipefail
action="$1"; expected_commit="$2"; branch="$3"
manifest_sha256="$4"; bundle_sha256="$5"
rollback_commit="$6"; authorization="$7"; authorization_consumed="$8"
source_repo="$9"; release_root="${10}"; service_dropin_root="${11}"; database_path="${12}"
source_mode="${13}"
per_role_rollback="${14}"; dry_run="${15}"
controller_commit="${16}"
IFS= read -r manifest_base64
IFS= read -r bundle_base64
manifest_base64="${manifest_base64%$'\r'}"
bundle_base64="${bundle_base64%$'\r'}"
temporary="$(mktemp -d /run/telegram-kol-action.XXXXXX)"
chmod 0700 "$temporary"
trap 'rm -rf -- "$temporary"' EXIT
printf '%s' "$manifest_base64" | base64 -d >"$temporary/action-manifest.json"
[ "$(sha256sum "$temporary/action-manifest.json" | awk '{print $1}')" = "$manifest_sha256" ]
case "$action" in
  stage)
    printf '%s' "$bundle_base64" | base64 -d >"$temporary/stager.tar"
    [ "$(sha256sum "$temporary/stager.tar" | awk '{print $1}')" = "$bundle_sha256" ]
    mkdir "$temporary/control"
    tar -xf "$temporary/stager.tar" -C "$temporary/control"
    PYTHONPATH="$temporary/control/src" EXPECTED_COMMIT="$expected_commit" BRANCH="$branch" \
      ACTION_MANIFEST="$temporary/action-manifest.json" SOURCE_REPO="$source_repo" \
      RELEASE_ROOT="$release_root" /opt/telegram-kol-analyzer/.venv/bin/python \
      "$temporary/control/deploy/telegram-kol-stage"
    ;;
  activate)
    if [ "$per_role_rollback" = "1" ]; then
      printf '%s' "$bundle_base64" | base64 -d >"$temporary/activation-controller.tar"
      [ "$(sha256sum "$temporary/activation-controller.tar" | awk '{print $1}')" = "$bundle_sha256" ]
      entries="$(tar -tf "$temporary/activation-controller.tar" | LC_ALL=C sort)"
      expected_entries="$(printf '%s\n' \
        src/ \
        src/telegram_kol_research/ \
        src/telegram_kol_research/__init__.py \
        src/telegram_kol_research/deployment_activation_quiescence_check.py \
        src/telegram_kol_research/deployment_active_write_check.py \
        src/telegram_kol_research/deployment_action_plan.py \
        src/telegram_kol_research/entry_revision_exchange_authority_contract.py \
        src/telegram_kol_research/runtime_deployment_identity.py \
        src/telegram_kol_research/scoped_release_activation.py | LC_ALL=C sort)"
      [ "$entries" = "$expected_entries" ] || { echo "Activation controller archive is unsafe." >&2; exit 4; }
      tar -tvf "$temporary/activation-controller.tar" \
        | awk 'substr($1, 1, 1) != "-" && substr($1, 1, 1) != "d" { exit 1 }' \
        || { echo "Activation controller archive contains a link or non-file entry." >&2; exit 4; }
      mkdir "$temporary/control"
      tar -xf "$temporary/activation-controller.tar" -C "$temporary/control"
      PYTHONPATH="$temporary/control/src" PYTHONDONTWRITEBYTECODE=1 \
        EXPECTED_COMMIT="$expected_commit" ROLLBACK_COMMIT= \
        ACTION_MANIFEST="$temporary/action-manifest.json" \
        ACTIVATION_AUTHORIZATION="$authorization" \
        ACTIVATION_AUTHORIZATION_CONSUMED="$authorization_consumed" \
        ACTIVATION_SOURCE_MODE="$source_mode" \
        ACTIVATION_CONTROLLER_COMMIT="$controller_commit" \
        ACTIVATION_CONTROLLER_BUNDLE_SHA256="$bundle_sha256" \
        ACTIVATION_DRY_RUN="$dry_run" RELEASE_ROOT="$release_root" \
        SERVICE_DROPIN_ROOT="$service_dropin_root" DATABASE_PATH="$database_path" \
        /opt/telegram-kol-analyzer/.venv/bin/python -B -m telegram_kol_research.scoped_release_activation
    else
      dispatcher_commit="$rollback_commit"
      if [ "$source_mode" = "stopped_legacy" ]; then
        dispatcher_commit="$expected_commit"
      fi
      updater="$release_root/$dispatcher_commit/deploy/telegram-kol-update"
      [ -x "$updater" ] || { echo "Immutable activation dispatcher is unavailable." >&2; exit 4; }
      DEPLOYMENT_ACTION=activate EXPECTED_COMMIT="$expected_commit" ROLLBACK_COMMIT="$rollback_commit" \
        ACTION_MANIFEST="$temporary/action-manifest.json" ACTIVATION_AUTHORIZATION="$authorization" \
        ACTIVATION_AUTHORIZATION_CONSUMED="$authorization_consumed" RELEASE_ROOT="$release_root" \
        ACTIVATION_SOURCE_MODE="$source_mode" ACTIVATION_DRY_RUN="$dry_run" \
        SERVICE_DROPIN_ROOT="$service_dropin_root" DATABASE_PATH="$database_path" "$updater"
    fi
    ;;
  *) echo "Remote action must be stage or activate." >&2; exit 2 ;;
esac
'@
    $encodedRemoteScript = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteScript))
    $remote = "bash -c `"`$(printf '%s' '$encodedRemoteScript' | base64 -d)`" -- " +
        "'$Action' '$ExpectedCommit' '$Branch' '$manifestSha' '$bundleSha' " +
        "'$RollbackCommit' '$ActivationAuthorization' " +
        "'$ActivationAuthorizationConsumed' '$SourceRepo' '$ReleaseRoot' " +
        "'$ServiceDropinRoot' '$DatabasePath' '$ActivationSourceMode' " +
        "'$([int]$perRoleRollback)' '$ActivationDryRun' " +
        "'$ActivationControllerCommit'"
    $payload = "$manifestBase64`n$bundleBase64"
    $payload | ssh -i $KeyPath $Server $remote
    if ($LASTEXITCODE -ne 0) {
        throw "Server $Action failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($bundlePath -and (Test-Path $bundlePath)) {
        Remove-Item -Force $bundlePath
    }
}
