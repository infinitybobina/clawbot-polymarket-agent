# Ночной сторож: перезапуск ClawBot при падении. Остановка утром в 07:00.
# Запуск перед сном: в отдельном окне PowerShell выполнить:
#   cd c:\Dev\Clawbot-polymarket-agent-clean
#   .\watch_overnight.ps1
# Окно можно свернуть. Утром к 07:00 скрипт сам выйдет; чтобы остановить раньше — закрой это окно.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$stopHour = 7   # прекратить перезапуски в 7:00
$restartDelaySec = 60

Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Watchdog started. Bot will restart on exit until $($stopHour):00. Close this window to stop."
Write-Host ""

while ($true) {
    $now = Get-Date
    if ($now.Hour -ge $stopHour -and $now.Hour -lt 22) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Stop time ($($stopHour):00). Exiting watchdog."
        break
    }

    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting ClawBot v2..."
    $p = Start-Process -FilePath "python" -ArgumentList "main_v2.py" -WorkingDirectory $scriptDir -PassThru -NoNewWindow
    $p.WaitForExit()
    $code = $p.ExitCode
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Process exited with code $code. Restarting in $restartDelaySec sec..."

    Start-Sleep -Seconds $restartDelaySec
}

Write-Host "Watchdog stopped."
