[CmdletBinding()]
param(
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot "data\auto_trading.pid.json"

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

if (-not (Test-Path $PidFile)) {
    Write-Host "auto-trading PID file not found; nothing to stop."
    exit 0
}

$metadata = Get-Content $PidFile -Raw | ConvertFrom-Json
$targetPid = [int]$metadata.pid
$processMetadata = Get-ProcessMetadata -ProcessId $targetPid

if ($null -eq $processMetadata) {
    Remove-Item -LiteralPath $PidFile -Force
    Write-Host "auto-trading runtime is not running; removed stale PID file."
    exit 0
}

if ($processMetadata.CommandLine -notmatch "main\.py") {
    throw "PID $targetPid is not a QuantBot Pro main.py process. Refusing to stop it."
}

Stop-Process -Id $targetPid -Force:$Force

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if ($null -eq (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
        break
    }
    Start-Sleep -Milliseconds 250
}

if ($null -ne (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
    throw "failed to stop auto-trading runtime PID $targetPid within timeout"
}

Remove-Item -LiteralPath $PidFile -Force
Write-Host "auto-trading runtime stopped"
Write-Host "PID: $targetPid"
