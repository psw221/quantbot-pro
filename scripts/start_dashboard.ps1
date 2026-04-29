[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [int]$Port = 8501,
    [switch]$Foreground
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot "data\dashboard.pid.json"
$StdOutLog = Join-Path $RepoRoot "logs\dashboard.stdout.log"
$StdErrLog = Join-Path $RepoRoot "logs\dashboard.stderr.log"
$DashboardPath = "monitor/dashboard_app.py"

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

function Test-DashboardCommandLine {
    param([string]$CommandLine)

    return $CommandLine -match "streamlit" -and $CommandLine -match "dashboard_app\.py"
}

if (Test-Path $PidFile) {
    $existing = Get-Content $PidFile -Raw | ConvertFrom-Json
    $processMetadata = Get-ProcessMetadata -ProcessId ([int]$existing.pid)
    if ($null -ne $processMetadata) {
        if (Test-DashboardCommandLine -CommandLine $processMetadata.CommandLine) {
            throw "dashboard app is already running with PID $($existing.pid)."
        }
        throw "PID file points to PID $($existing.pid), but it is not the dashboard app. Refusing to overwrite it."
    }
    Remove-Item -LiteralPath $PidFile -Force
}

if ($Foreground) {
    Set-Location $RepoRoot
    & $PythonExe "-m" "streamlit" "run" $DashboardPath "--server.port" $Port
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
    -ArgumentList @("-m", "streamlit", "run", $DashboardPath, "--server.port", [string]$Port, "--server.headless", "true") `
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
    port = $Port
    url = "http://localhost:$Port"
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8

Write-Host "dashboard app started"
Write-Host "PID: $($process.Id)"
Write-Host "URL: http://localhost:$Port"
Write-Host "stdout: $StdOutLog"
Write-Host "stderr: $StdErrLog"
