@echo off
title LinX - Approvals
rem Double-click (or tap over Chrome Remote Desktop) to approve or reject tasks waiting on the owner.
powershell.exe -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Approve-Linx.ps1"
