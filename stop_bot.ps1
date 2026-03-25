# Stop ClawBot v2 (python main_v2.py). Run: powershell -File stop_bot.ps1
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like "*main_v2*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "Stopped PID $($_.ProcessId)" }
Write-Host "Done. Run: python main_v2.py"
