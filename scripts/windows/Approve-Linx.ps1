# LinX approvals menu: a numbered list of tasks waiting for the owner; pick one, then A or R.
# Built to be driven from a phone over Chrome Remote Desktop (or any remote session): no typing of task ids.
# Decisions run `linx approve` / `linx reject` on this machine, so the owner key signs them as usual.
#
# Reads the same user-level settings as Start-LinxService.ps1:
#   UNISYNC_SUPABASE_URL / UNISYNC_SUPABASE_KEY (or LINX_LEDGER), LINX_PYTHON (default "python").

$ErrorActionPreference = "Stop"

function Get-Setting([string]$Name, [string]$Default = "") {
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not $value) { $value = [Environment]::GetEnvironmentVariable($Name, "User") }
    if (-not $value) { $value = $Default }
    return $value
}

$python = Get-Setting "LINX_PYTHON" "python"
$ledger = Get-Setting "LINX_LEDGER"
$env:UNISYNC_SUPABASE_URL = Get-Setting "UNISYNC_SUPABASE_URL"
$env:UNISYNC_SUPABASE_KEY = Get-Setting "UNISYNC_SUPABASE_KEY"
foreach ($name in "UNISYNC_OWNER_KEY", "UNISYNC_OWNER_FROM") {
    $v = Get-Setting $name
    if ($v) { Set-Item -Path "Env:$name" -Value $v }
}
$ledgerArgs = @()
if ($ledger) { $ledgerArgs = @("--ledger", $ledger) }

function Invoke-Linx([string[]]$LinxArgs) {
    & $python -m linx @LinxArgs @ledgerArgs 2>&1
}

# `linx pending` prints "<task id>  <title>  (from <who>)" per task, then a count line.
$rowPattern = '^(?<id>\S+)  (?<title>.*)  \(from (?<who>[^)]*)\)$'

while ($true) {
    Clear-Host
    Write-Host "LinX approvals" -ForegroundColor Cyan
    Write-Host ""
    $out = @(Invoke-Linx @("pending"))
    $tasks = @()
    foreach ($line in $out) {
        if ("$line" -match $rowPattern) { $tasks += [pscustomobject]@{ Id = $Matches.id; Title = $Matches.title; Who = $Matches.who } }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($out -join "`n") -ForegroundColor Red
        Read-Host "`nLinX could not read the ledger. Press Enter to close"
        exit 1
    }
    if ($tasks.Count -eq 0) {
        Write-Host "Nothing is waiting on you." -ForegroundColor Green
        Read-Host "`nPress Enter to close"
        exit 0
    }
    for ($i = 0; $i -lt $tasks.Count; $i++) {
        Write-Host ("  [{0}]  {1}" -f ($i + 1), $tasks[$i].Title) -ForegroundColor White
        Write-Host ("        from {0}  -  {1}" -f $tasks[$i].Who, $tasks[$i].Id) -ForegroundColor DarkGray
    }
    $pick = Read-Host "`nType a number, then Enter (just Enter to close)"
    if (-not $pick) { exit 0 }
    $n = 0
    if (-not [int]::TryParse($pick, [ref]$n) -or $n -lt 1 -or $n -gt $tasks.Count) { continue }
    $task = $tasks[$n - 1]

    Write-Host ("`n{0}" -f $task.Title) -ForegroundColor Cyan
    $choice = Read-Host "A = approve,  R = reject,  Enter = back"
    if ($choice -match '^[aA]') {
        Invoke-Linx @("approve", $task.Id) | Write-Host
    } elseif ($choice -match '^[rR]') {
        $why = Read-Host "Reason (Enter for 'rejected from phone')"
        if (-not $why) { $why = "rejected from phone" }
        Invoke-Linx @("reject", $task.Id, "--reason", $why) | Write-Host
    } else {
        continue
    }
    Read-Host "`nPress Enter to go back to the list"
}
