param([ValidateSet('api','worker')][string]$Role = 'api')
$ErrorActionPreference = 'Stop'
$animFlowRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $animFlowRoot
# An isolated data directory preserves earlier development/test history.
$env:ANIMFLOW_DATA_DIR = Join-Path $animFlowRoot 'data\recording'
$env:ANIMFLOW_DB_PATH = Join-Path $env:ANIMFLOW_DATA_DIR 'animflow.db'
$env:ANIMFLOW_ARTIFACTS_DIR = Join-Path $env:ANIMFLOW_DATA_DIR 'artifacts'
$env:ANIMFLOW_LOGS_DIR = Join-Path $env:ANIMFLOW_DATA_DIR 'logs'
$env:ANIMFLOW_HEARTBEAT_S = '0.5'
$env:ANIMFLOW_LEASE_S = '3'
$env:ANIMFLOW_POLL_S = '0.2'
$animFlowPython = Join-Path $animFlowRoot '.venv\Scripts\python.exe'
if ($Role -eq 'api') {
    & $animFlowPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
} else {
    & $animFlowPython -m app.worker
}
exit $LASTEXITCODE
