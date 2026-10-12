# Requires Windows PowerShell 5.1+ (built into Windows 10/11).
# Runs the node agent; desktop/run_agent.py validates and loads the private env file.
# Usage:
#   powershell -ExecutionPolicy Bypass -File lastcore-node-agent.ps1 -EnvFile C:\Users\me\AppData\Local\LastCore\env
param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [string]$VenvDir = (Join-Path $PSScriptRoot "..\..\.venv")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $EnvFile)) {
    Write-Error "env file not found: $EnvFile"
    exit 4
}

$repoDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$pythonExe = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Error "venv python not found: $pythonExe (create it with: py -3.12 -m venv $VenvDir; $VenvDir\Scripts\pip install -r $repoDir\requirements-dev.txt)"
    exit 4
}

Set-Location $repoDir
& $pythonExe (Join-Path $PSScriptRoot "..\run_agent.py") --env-file $EnvFile
exit $LASTEXITCODE
