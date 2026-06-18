# Start exactly one DHL dashboard instance (kills stale app.py processes first).
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'dhl_dashboard.*app\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Start-Sleep -Seconds 2

$venv = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venv)) {
    Write-Error "Missing $venv — create the venv first."
    exit 1
}

Write-Host "Starting DHL Fleet Health at http://127.0.0.1:8050/mix"
Write-Host "Sidebar should show: MiX Health + Build: mix-health-2026-06b"
& $venv app.py
