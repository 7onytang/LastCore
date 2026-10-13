# Removes the per-user Task Scheduler job installed by install-task.ps1.
param(
    [string]$TaskName = "LastCoreNodeAgent"
)

$ErrorActionPreference = "Stop"
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Uninstalled scheduled task '$TaskName'."
