@echo off
rem brodi: start / stop / status / log / restart / tray on Windows 11.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0console.ps1" %*
