# Register voice-journal tasks in Windows Task Scheduler.
# Run in an elevated PowerShell. Remove with uninstall-autostart.ps1.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $root '.venv\Scripts\pythonw.exe'
$venvPyConsole = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPy)) { throw "venv Python not found: $venvPy. Run setup.ps1 first." }

function Register-VJTask {
    param([string]$Name, [string]$Exe, [string]$Args, [bool]$Daily = $false)

    $action = New-ScheduledTaskAction -Execute $Exe -Argument $Args -WorkingDirectory $root
    if ($Daily) {
        $trigger = New-ScheduledTaskTrigger -Daily -At '23:30'
    } else {
        $trigger = New-ScheduledTaskTrigger -AtLogOn
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0) `
        -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered task: $Name"
}

Register-VJTask -Name 'voice-journal-recorder' `
    -Exe $venvPy -Args "src\recorder.py"

Register-VJTask -Name 'voice-journal-transcriber' `
    -Exe $venvPy -Args "src\transcriber.py"

Register-VJTask -Name 'voice-journal-daily-summary' `
    -Exe $venvPyConsole -Args "src\daily_summary.py --rerun-pending" -Daily $true

Write-Host ""
Write-Host "Done. Open Task Scheduler (taskschd.msc) to review the three tasks."
Write-Host "Run them manually for a smoke test, or wait for the next sign-in / 23:30."
