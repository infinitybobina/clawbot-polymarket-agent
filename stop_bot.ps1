# Stop ClawBot v2 reliably on Windows (python + watchdog wrappers).
# Run:
#   powershell -ExecutionPolicy Bypass -File .\stop_bot.ps1
# or simply:
#   .\stop_bot.cmd

$stopped = 0

# 1) Stop python main loop processes.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*main_v2.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped python PID $($_.ProcessId)"
        $stopped++
    }

# 2) Stop watchdog/session PowerShell wrappers that could auto-restart python.
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like "*run_session_hours.ps1*" -or
        $_.CommandLine -like "*watch_overnight.ps1*"
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped wrapper PID $($_.ProcessId)"
        $stopped++
    }

if ($stopped -eq 0) {
    Write-Host "No running bot/wrapper processes found."
} else {
    Write-Host "Done. Stopped $stopped process(es)."
}
