# export-push.ps1 — scan + delta-export + push to the VPS inbox. BMF (Windows).
# Fired by the Claude Code SessionEnd hook (detached); safe to run by hand.
# Debounced: exits quietly if a push happened in the last 15 minutes.
#
# Failure model: export lands in a local spool dir BEFORE any network is
# touched; every spooled file is pushed and only deleted on scp success, so a
# dead VPS just means the next session retries the same files.

$ErrorActionPreference = 'Stop'
$env:TOKEN_DASHBOARD_BOX = 'BMF'

$repo    = Split-Path -Parent $PSScriptRoot
$scratch = if ($env:SCRATCH_DIR) { $env:SCRATCH_DIR } else { Join-Path $env:USERPROFILE 'Documents\Claude\Projects\_scratch' }
$spool   = Join-Path $scratch 'token-dashboard-spool'
$stamp   = Join-Path $scratch 'token-dashboard-lastpush'
$log     = Join-Path $scratch 'token-dashboard-push.log'
$inbox   = 'vps:~/Claude/Projects/_scratch/token-dashboard-inbox/'

New-Item -ItemType Directory -Force $spool | Out-Null

if ((Test-Path $stamp) -and ((Get-Date) - (Get-Item $stamp).LastWriteTime).TotalMinutes -lt 15) {
    exit 0
}

function Log($msg) {
    Add-Content -Encoding utf8 $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
}

try {
    $out = Join-Path $spool ("td-BMF-{0}.json.gz" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Set-Location $repo
    py -3 cli.py export --out $out | Out-Null

    $pushed = 0
    foreach ($f in Get-ChildItem $spool -Filter '*.json.gz') {
        scp -q $f.FullName $inbox
        if ($LASTEXITCODE -ne 0) { throw "scp failed for $($f.Name)" }
        Remove-Item $f.FullName
        $pushed++
    }
    New-Item -ItemType File -Force $stamp | Out-Null
    Log "pushed $pushed file(s)"
} catch {
    Log "ERROR: $_"
    exit 1
}
