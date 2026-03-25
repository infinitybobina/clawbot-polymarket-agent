# Длительный прогон ClawBot v2: N часов, при выходе процесса — пауза и перезапуск.
# Пример: .\run_session_hours.ps1 -Hours 15
# Остановка: закрыть это окно или Ctrl+C в нём.

param(
    [ValidateRange(1, 72)]
    [int]$Hours = 15,
    [int]$RestartDelaySec = 60
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$deadline = (Get-Date).AddHours($Hours)
Write-Host ""
Write-Host "=== ClawBot long session ===" -ForegroundColor Cyan
Write-Host "Until (local): $($deadline.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Duration: $Hours h | Restart delay: $RestartDelaySec s"
Write-Host "Log file: $scriptDir\clawbot_v2_run.log"
Write-Host "Session marker: $scriptDir\bot_session.json (updated each process start)"
Write-Host ""

while ((Get-Date) -lt $deadline) {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting: python main_v2.py"
    $p = Start-Process -FilePath "python" -ArgumentList "main_v2.py" -WorkingDirectory $scriptDir -PassThru -NoNewWindow
    $p.WaitForExit()
    $code = $p.ExitCode
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] python exited with code $code"
    if ((Get-Date) -ge $deadline) {
        break
    }
    Write-Host "Restarting in $RestartDelaySec s..."
    Start-Sleep -Seconds $RestartDelaySec
}

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Session window ended ($Hours h)."
