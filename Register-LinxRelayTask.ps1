# LinX - Windows logon persistence for the relay and memory loops
# (same pattern as HORIZONTAL's Register-CitadelScheduledTask.ps1)
param(
    [switch]$SetSecrets,
    [switch]$Register,
    [switch]$Unregister,
    [switch]$Status,
    [switch]$Run,
    [switch]$RunMemory,
    [switch]$RunNightly,
    [switch]$SelfTest
)

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ServiceScript = Join-Path $RepoRoot "scripts\windows\Start-LinxService.ps1"
$LogDir = Join-Path $env:LOCALAPPDATA "LinX"
$StartupDir = [Environment]::GetFolderPath("Startup")
# One logon task per loop: name -> service. LinX-Nightly is a daily one-shot (see $NightlyTask).
$Services = [ordered]@{ "LinX-Relay" = "relay"; "LinX-Memory" = "memory" }
$NightlyTask = "LinX-Nightly"

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Set-UserVar([string]$Name, [string]$Value) {
    if ($Value) { [Environment]::SetEnvironmentVariable($Name, $Value, "User") }
}

function Get-Action([string]$Service) {
    return "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ServiceScript`" -Service $Service"
}

function Show-Status {
    foreach ($task in $Services.Keys) {
        $service = $Services[$task]
        $launcher = Join-Path $StartupDir "$task.cmd"
        schtasks /Query /TN $task /FO LIST 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[+] $task is REGISTERED (Task Scheduler)." -ForegroundColor Green
        } elseif (Test-Path $launcher) {
            Write-Host "[+] $task is ACTIVE via Startup folder: $launcher" -ForegroundColor Green
        } else {
            Write-Host "[!] $task is not registered. Run .\Register-LinxRelayTask.ps1 -Register" -ForegroundColor Yellow
        }
        $log = Join-Path $LogDir "$service.log"
        if (Test-Path $log) {
            Get-Content $log -Tail 3 | ForEach-Object { Write-Host "     $_" }
        }
    }
    schtasks /Query /TN $NightlyTask /FO LIST 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[+] $NightlyTask is REGISTERED (daily)." -ForegroundColor Green
    } else {
        Write-Host "[!] $NightlyTask is not registered (needs Task Scheduler; run -Register)." -ForegroundColor Yellow
    }
    $latest = Get-ChildItem -Path $LogDir -Filter "digest-*.md" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($latest) { Write-Host "     latest digest: $($latest.FullName)" }
    Write-Host "[*] Settings (user environment):" -ForegroundColor Cyan
    foreach ($name in "VORTEX_OPERATOR_TOKEN", "UNISYNC_SUPABASE_URL", "UNISYNC_SUPABASE_KEY", "LINX_VORTEX_URL",
                      "LINX_MEMORY_AGENT", "LINX_MEMORY_EVERY", "LINX_NIGHTLY_AT", "LINX_NIGHTLY_TIMEOUT", "LINX_LEDGER") {
        $set = [bool][Environment]::GetEnvironmentVariable($name, "User")
        Write-Host ("   {0,-22} {1}" -f $name, $(if ($set) { "set" } else { "-" }))
    }
}

if ($SetSecrets) {
    Write-Host "[*] Values are stored as user environment variables. Leave blank to keep the current value." -ForegroundColor Cyan
    Set-UserVar "VORTEX_OPERATOR_TOKEN" (Read-Secret "Vortex operator token")
    Set-UserVar "UNISYNC_SUPABASE_URL" (Read-Host "Supabase URL (https://<ref>.supabase.co)")
    Set-UserVar "UNISYNC_SUPABASE_KEY" (Read-Secret "Supabase service-role key")
    Set-UserVar "LINX_VORTEX_URL" (Read-Host "Vortex URL [http://127.0.0.1:8000]")
    Set-UserVar "LINX_MEMORY_AGENT" (Read-Host "Memory agent [coordinator-bot]")
    Set-UserVar "LINX_MEMORY_EVERY" (Read-Host "Memory consolidation interval in seconds [300]")
    Set-UserVar "LINX_NIGHTLY_AT" (Read-Host "Nightly run time HH:MM [02:17]")
    Write-Host "[+] Saved." -ForegroundColor Green
    Show-Status
    exit 0
}

if ($Unregister) {
    foreach ($task in $Services.Keys) {
        schtasks /Delete /TN $task /F 2>&1 | Out-Null
        $launcher = Join-Path $StartupDir "$task.cmd"
        if (Test-Path $launcher) { Remove-Item $launcher -Force }
    }
    schtasks /Delete /TN $NightlyTask /F 2>&1 | Out-Null
    Write-Host "[+] Unregistered LinX-Relay, LinX-Memory and LinX-Nightly." -ForegroundColor Green
    exit 0
}

if ($Register) {
    foreach ($task in $Services.Keys) {
        $action = Get-Action $Services[$task]
        schtasks /Create /TN $task /TR $action /SC ONLOGON /F 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[+] Registered [$task] in Task Scheduler (runs at logon)." -ForegroundColor Green
        } else {
            $launcher = Join-Path $StartupDir "$task.cmd"
            $cmd = "@echo off`r`nstart `"`" $action`r`n"
            Set-Content -Path $launcher -Value $cmd -Encoding ASCII
            Write-Host "[!] Task Scheduler needs elevation; created Startup launcher $launcher" -ForegroundColor Yellow
        }
    }
    $at = [Environment]::GetEnvironmentVariable("LINX_NIGHTLY_AT", "User")
    if (-not $at) { $at = "02:17" }
    schtasks /Create /TN $NightlyTask /TR (Get-Action "nightly") /SC DAILY /ST $at /F 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[+] Registered [$NightlyTask] daily at $at." -ForegroundColor Green
    } else {
        Write-Host "[!] Could not register [$NightlyTask]: daily tasks need Task Scheduler (try an elevated shell)." -ForegroundColor Yellow
    }
    Show-Status
    exit 0
}

if ($SelfTest) {
    # Read-only: relays a built-in feed, then (if configured) reads the real Vortex and Supabase.
    foreach ($name in "VORTEX_OPERATOR_TOKEN", "UNISYNC_SUPABASE_URL", "UNISYNC_SUPABASE_KEY", "LINX_VORTEX_URL") {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($value -and -not [Environment]::GetEnvironmentVariable($name, "Process")) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    if (-not $env:LINX_VORTEX_URL) { $env:LINX_VORTEX_URL = "http://127.0.0.1:8000" }
    $python = [Environment]::GetEnvironmentVariable("LINX_PYTHON", "User")
    if (-not $python) { $python = "python" }
    & $python -m linx selftest --root (Split-Path -Parent $RepoRoot)
    exit $LASTEXITCODE
}

if ($Run) {
    & powershell.exe -ExecutionPolicy Bypass -File "$ServiceScript" -Service relay -Foreground
    exit $LASTEXITCODE
}

if ($RunMemory) {
    & powershell.exe -ExecutionPolicy Bypass -File "$ServiceScript" -Service memory -Foreground
    exit $LASTEXITCODE
}

if ($RunNightly) {
    & powershell.exe -ExecutionPolicy Bypass -File "$ServiceScript" -Service nightly -Foreground
    exit $LASTEXITCODE
}

Show-Status
Write-Host "`nUsage:" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -SetSecrets   # store token, Supabase URL/key, memory agent (once)" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -SelfTest     # read-only check: UniSync, relay, Vortex, Supabase" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Run          # run the relay in this window" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -RunMemory    # run the memory loop in this window" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -RunNightly   # one nightly run now (plan, agents, digest)" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Register     # relay + memory at logon, nightly daily" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Unregister   # remove both" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Status       # persistence, settings, last log lines" -ForegroundColor DarkGray
