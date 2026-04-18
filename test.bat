@echo off
:: forward to PowerShell version; ensures invocation even if one command fails
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0test.ps1"

