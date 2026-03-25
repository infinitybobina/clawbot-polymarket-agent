@echo off
cd /d "%~dp0"
start "ClawBot night watchdog" /Min powershell -ExecutionPolicy Bypass -NoExit -File "%~dp0watch_overnight.ps1"
