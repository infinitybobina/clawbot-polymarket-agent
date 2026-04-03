@echo off
setlocal
cd /d "%~dp0"
set HOURS=%1
if "%HOURS%"=="" set HOURS=15
powershell -NoProfile -ExecutionPolicy Bypass -File ".\run_session_hours.ps1" -Hours %HOURS%
endlocal
