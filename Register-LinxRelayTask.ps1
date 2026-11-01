# LinX live relay - Windows logon persistence (same pattern as HORIZONTAL's Register-CitadelScheduledTask.ps1)
param(
    [switch]$SetSecrets,
    [switch]$Register,
    [switch]$Unregister,
    [switch]$Status,
    [switch]$Run,
    [switch]$SelfTest
)

$TaskName = "LinX-Relay"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ServiceScript = Join-Path $RepoRoot "scripts\windows\Start-LinxRelay.ps1"
$LogFile = Join-Path $env:LOCALAPPDATA "LinX\relay.log"
$StartupLauncher = Join-Path ([Environment]::GetFolderPath("Startup")) "LinX-Relay.cmd"

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Set-UserVar([string]$Name, [string]$Value) {
    if ($Value) { [Environment]::SetEnvironmentVariable($Name, $Value, "User") }
}

function Show-Status {
    Write-Host "[*] LinX relay persistence for [$TaskName]:" -ForegroundColor Cyan
    schtasks /Query /TN $TaskName /FO LIST 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[+] Scheduled Task is REGISTERED." -ForegroundColor Green
    } elseif (Test-Path $StartupLauncher) {
        Write-Host "[+] Startup-folder launcher is ACTIVE: $StartupLauncher" -ForegroundColor Green
    } else {
        Write-Host "[!] Not registered. Run .\Register-LinxRelayTask.ps1 -Register" -ForegroundColor Yellow
    }
    foreach ($name in "VORTEX_OPERATOR_TOKEN", "UNISYNC_SUPABASE_URL", "UNISYNC_SUPABASE_KEY", "LINX_VORTEX_URL", "LINX_LEDGER") {
        $set = [bool][Environment]::GetEnvironmentVariable($name, "User")
        Write-Host ("   {0,-22} {1}" -f $name, $(if ($set) { "set" } else { "-" }))
    }
    if (Test-Path $LogFile) {
        Write-Host "[*] Last log lines ($LogFile):" -ForegroundColor Cyan
        Get-Content $LogFile -Tail 5 | ForEach-Object { Write-Host "   $_" }
    }
}

if ($SetSecrets) {
    Write-Host "[*] Values are stored as user environment variables. Leave blank to keep the current value." -ForegroundColor Cyan
    Set-UserVar "VORTEX_OPERATOR_TOKEN" (Read-Secret "Vortex operator token")
    Set-UserVar "UNISYNC_SUPABASE_URL" (Read-Host "Supabase URL (https://<ref>.supabase.co)")
    Set-UserVar "UNISYNC_SUPABASE_KEY" (Read-Secret "Supabase service-role key")
    Set-UserVar "LINX_VORTEX_URL" (Read-Host "Vortex URL [http://127.0.0.1:8000]")
    Write-Host "[+] Saved." -ForegroundColor Green
    Show-Status
    exit 0
}

if ($Unregister) {
    schtasks /Delete /TN $TaskName /F 2>&1 | Out-Null
    if (Test-Path $StartupLauncher) { Remove-Item $StartupLauncher -Force }
    Write-Host "[+] Unregistered [$TaskName]." -ForegroundColor Green
    exit 0
}

if ($Register) {
    $action = "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ServiceScript`""
    schtasks /Create /TN $TaskName /TR $action /SC ONLOGON /F 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[+] Registered [$TaskName] in Task Scheduler (runs at logon)." -ForegroundColor Green
    } else {
        Write-Host "[!] Task Scheduler needs elevation. Using the Startup folder instead." -ForegroundColor Yellow
        $cmd = "@echo off`r`nstart `"`" powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ServiceScript`"`r`n"
        Set-Content -Path $StartupLauncher -Value $cmd -Encoding ASCII
        Write-Host "[+] Created $StartupLauncher" -ForegroundColor Green
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
    & powershell.exe -ExecutionPolicy Bypass -File "$ServiceScript" -Foreground
    exit $LASTEXITCODE
}

Show-Status
Write-Host "`nUsage:" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -SetSecrets   # store token, Supabase URL/key (once)" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -SelfTest     # read-only check: UniSync, relay, Vortex, Supabase" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Run          # run in this window to check it works" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Register     # start at every logon" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Unregister   # remove" -ForegroundColor DarkGray
Write-Host "  .\Register-LinxRelayTask.ps1 -Status       # persistence, settings, last log lines" -ForegroundColor DarkGray
