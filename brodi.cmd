@echo off
rem brodi: start / stop / status / log / restart the voice assistant on Windows 11.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0console.ps1" %*
