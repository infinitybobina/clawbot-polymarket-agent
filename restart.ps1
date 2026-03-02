# Stop ClawBot v2, then start it again. Run: .\restart.bat or powershell -File restart.ps1
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$killed = 0
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*main_v2*" } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped PID $($_.ProcessId)"
    $killed++
}
if ($killed -eq 0) { Write-Host "No running bot found." }
Start-Sleep -Seconds 1
Write-Host "Starting ClawBot v2..."
Start-Process python -ArgumentList "main_v2.py" -WorkingDirectory $scriptDir
Write-Host "Done."
