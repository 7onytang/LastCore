# Registers a per-user Task Scheduler job that starts the node agent at logon.
# Run from an elevated or normal PowerShell as the user who should run the node:
#   powershell -ExecutionPolicy Bypass -File install-task.ps1 -EnvFile C:\Users\me\AppData\Local\LastCore\env
param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [string]$TaskName = "LastCoreNodeAgent"
)

$ErrorActionPreference = "Stop"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\lastcore-node-agent.ps1`" -EnvFile `"$EnvFile`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "LastCore node agent (LC-12 desktop runtime)" -Force
Write-Host "Installed. Start now with: Start-ScheduledTask -TaskName $TaskName"
