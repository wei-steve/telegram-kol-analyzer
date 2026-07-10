Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Check([string] $Message) { Write-Host "[OK] $Message" }
function Write-Warn([string] $Message) { Write-Warning $Message }
function Write-Fail([string] $Message) { Write-Host "[FAIL] $Message" -ForegroundColor Red }

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$failures = 0

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail 'Git is required.'
    exit 1
}

$ignorePath = Join-Path $repoRoot '.gitignore'
$ignoreLines = Get-Content -LiteralPath $ignorePath
foreach ($rule in @('.venv/', 'config/*.env', 'data/*.session', 'data/', '*.log')) {
    if ($ignoreLines -contains $rule) { Write-Check "Ignore rule present: $rule" }
    else { Write-Fail "Missing ignore rule: $rule"; $failures++ }
}

$branch = (& git -C $repoRoot branch --show-current).Trim()
Write-Check "Branch: $branch"
& git -C $repoRoot status --short

$sensitiveName = '(?i)(\.env$|\.session|api[_-]?key|token|secret|credential|passphrase)'
& git -C $repoRoot status --short --untracked-files=all | ForEach-Object {
    $path = $_.Substring(3).Trim('"')
    if ($path -match $sensitiveName) { Write-Warn "Untracked sensitive-looking path: $path" }
}

$secretPattern = '(?i)(api[_-]?key|token|secret|password|passphrase)\s*[:=]\s*(?<value>"[^"]+"|''[^'']+''|[A-Za-z0-9_./+-]{16,})'
& git -C $repoRoot ls-files | ForEach-Object {
    $relativePath = $_
    if ($relativePath -match '^(\.git/|\.venv/|\.pytest_cache/|videos/)') { return }
    $fullPath = Join-Path $repoRoot $relativePath
    if (-not (Test-Path -LiteralPath $fullPath)) { return }
    if ((Get-Item -LiteralPath $fullPath).Length -gt 1MB) { return }
    $bytes = [System.IO.File]::ReadAllBytes($fullPath)
    if ($bytes[0..([Math]::Min($bytes.Length - 1, 4095))] -contains 0) { return }
    $lineNumber = 0
    Get-Content -LiteralPath $fullPath -Encoding UTF8 | ForEach-Object {
        $lineNumber++
        $match = [regex]::Match($_, $secretPattern)
        $candidate = $match.Groups['value'].Value
        if ($match.Success -and $candidate -notmatch '(?i)(your_|example|placeholder|\.\.\.)') {
            Write-Fail "Suspected credential: ${relativePath}:$lineNumber"
            $script:failures++
        }
    }
}

if ($failures -gt 0) { Write-Fail "Preflight found $failures blocking issue(s)."; exit 1 }
Write-Check 'Migration preflight completed without blocking findings.'
exit 0
