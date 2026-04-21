[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [switch]$Foreground
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot "data\auto_trading.pid.json"
$StdOutLog = Join-Path $RepoRoot "logs\auto_trading.stdout.log"
$StdErrLog = Join-Path $RepoRoot "logs\auto_trading.stderr.log"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PidFile) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StdOutLog) | Out-Null

function Get-ProcessMetadata {
    param([int]$ProcessId)

    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $processInfo) {
        return $null
    }

    return [pscustomobject]@{
        ProcessId = $ProcessId
        CommandLine = [string]$processInfo.CommandLine
    }
}

if (Test-Path $PidFile) {
    $existing = Get-Content $PidFile -Raw | ConvertFrom-Json
    $processMetadata = Get-ProcessMetadata -ProcessId ([int]$existing.pid)
    if ($null -ne $processMetadata -and $processMetadata.CommandLine -match "main\.py") {
        throw "auto-trading runtime is already running with PID $($existing.pid)."
    }
    Remove-Item -LiteralPath $PidFile -Force
}

$settingsJson = @'
from core.settings import get_settings
import json

settings = get_settings()
print(json.dumps({
    "env": settings.env.value,
    "auto_trading_enabled": settings.auto_trading.enabled,
    "telegram_enabled": settings.monitor.telegram.enabled,
}))
'@ | & $PythonExe -

if (-not $settingsJson) {
    throw "failed to read runtime settings"
}

$settings = $settingsJson | ConvertFrom-Json
if ($settings.env -ne "vts") {
    throw "config/config.yaml must keep env=vts for this start script."
}
if (-not [bool]$settings.auto_trading_enabled) {
    throw "config/config.yaml must set auto_trading.enabled=true before starting auto-trading."
}

if ($Foreground) {
    Set-Location $RepoRoot
    & $PythonExe "main.py"
    exit $LASTEXITCODE
}

if (Test-Path $StdOutLog) {
    Remove-Item -LiteralPath $StdOutLog -Force
}
if (Test-Path $StdErrLog) {
    Remove-Item -LiteralPath $StdErrLog -Force
}

$process = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList "main.py" `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $StdOutLog `
    -RedirectStandardError $StdErrLog `
    -WindowStyle Hidden `
    -PassThru

$metadata = [pscustomobject]@{
    pid = $process.Id
    started_at = (Get-Date).ToString("o")
    stdout_log = $StdOutLog
    stderr_log = $StdErrLog
    env = $settings.env
    auto_trading_enabled = [bool]$settings.auto_trading_enabled
    telegram_enabled = [bool]$settings.telegram_enabled
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8

Write-Host "auto-trading runtime started"
Write-Host "PID: $($process.Id)"
Write-Host "stdout: $StdOutLog"
Write-Host "stderr: $StdErrLog"
