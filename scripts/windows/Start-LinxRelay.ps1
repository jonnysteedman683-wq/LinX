# LinX live relay supervisor: keeps `linx relay-live` running on this machine.
# Started at logon by Register-LinxRelayTask.ps1, or by hand via START_LINX_RELAY.bat.
#
# Reads user-level environment variables (set them with Register-LinxRelayTask.ps1 -SetSecrets):
#   VORTEX_OPERATOR_TOKEN   Vortex operator bearer token            (required)
#   UNISYNC_SUPABASE_URL    https://<ref>.supabase.co               (required unless LINX_LEDGER is set)
#   UNISYNC_SUPABASE_KEY    Supabase service-role key               (required unless LINX_LEDGER is set)
#   LINX_VORTEX_URL         Vortex server root, default http://127.0.0.1:8000
#   LINX_LEDGER             optional local JSONL ledger instead of Supabase
#   LINX_PYTHON             optional python.exe with linx[unisync] installed, default "python"
#
# Exit code 2 from the relay means a config or data problem (bad token, bad URL, malformed event):
# the supervisor stops instead of hammering Vortex. Any other exit restarts after a growing delay.
param([switch]$Foreground)

$ErrorActionPreference = "Stop"
$LogDir = Join-Path $env:LOCALAPPDATA "LinX"
$LogFile = Join-Path $LogDir "relay.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    if ($Foreground) { Write-Host $line }
}

function Get-Setting([string]$Name, [string]$Default = "") {
    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not $value) { $value = [Environment]::GetEnvironmentVariable($Name, "User") }
    if (-not $value) { $value = $Default }
    return $value
}

$python = Get-Setting "LINX_PYTHON" "python"
$vortexUrl = Get-Setting "LINX_VORTEX_URL" "http://127.0.0.1:8000"
$ledger = Get-Setting "LINX_LEDGER"
$env:VORTEX_OPERATOR_TOKEN = Get-Setting "VORTEX_OPERATOR_TOKEN"
$env:UNISYNC_SUPABASE_URL = Get-Setting "UNISYNC_SUPABASE_URL"
$env:UNISYNC_SUPABASE_KEY = Get-Setting "UNISYNC_SUPABASE_KEY"

$missing = @()
if (-not $env:VORTEX_OPERATOR_TOKEN) { $missing += "VORTEX_OPERATOR_TOKEN" }
if (-not $ledger -and -not $env:UNISYNC_SUPABASE_URL) { $missing += "UNISYNC_SUPABASE_URL" }
if (-not $ledger -and -not $env:UNISYNC_SUPABASE_KEY) { $missing += "UNISYNC_SUPABASE_KEY" }
if ($missing.Count -gt 0) {
    Write-Log ("[!] Not starting: missing " + ($missing -join ", ") + ". Run Register-LinxRelayTask.ps1 -SetSecrets.")
    exit 2
}

$relayArgs = @("-m", "linx", "relay-live", "--vortex-url", $vortexUrl, "--log-file", $LogFile)
if ($ledger) { $relayArgs += @("--ledger", $ledger) }
$target = if ($ledger) { $ledger } else { "Supabase" }

$delay = 10
while ($true) {
    Write-Log "[*] Starting relay: $vortexUrl -> $target"
    $started = Get-Date
    # Python writes its own UTF-8 log; no PowerShell redirection, so stderr cannot trip ErrorActionPreference.
    $ErrorActionPreference = "Continue"
    & $python @relayArgs | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($code -eq 0) { Write-Log "[+] Relay stopped cleanly."; exit 0 }
    if ($code -eq 2) { Write-Log "[!] Relay stopped on a config or data error (exit 2). Fix it, then restart."; exit 2 }
    if (((Get-Date) - $started).TotalMinutes -ge 10) { $delay = 10 }
    Write-Log "[!] Relay exited with code $code. Restarting in $delay s."
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 600)
}
