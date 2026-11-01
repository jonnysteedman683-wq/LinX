@echo off
title LinX - Live Vortex to UniSync Relay
echo ================================================================================
echo  LinX live relay: Vortex task events -^> UniSync ledger
echo  Settings: Register-LinxRelayTask.ps1 -SetSecrets   Log: %%LOCALAPPDATA%%\LinX\relay.log
echo  Press Ctrl+C to stop.
echo ================================================================================
powershell.exe -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Start-LinxRelay.ps1" -Foreground
pause
