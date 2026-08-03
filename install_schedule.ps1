# install_schedule.ps1  --  LOCKBOT's Windows Task Scheduler setup
#
# WHAT ALREADY EXISTED
#   "LockBotController" was already registered and correct: it fires at
#   logon, runs the venv interpreter against lockbot_controller.py, and
#   restarts up to 999 times at one-minute intervals if the process dies.
#   That is why LOCKBOT comes back after a reboot. This script leaves it
#   alone.
#
# WHAT WAS MISSING, AND WHY IT MATTERED
#   Nothing rebuilt universe.csv. market_scanner.py reads its symbol list
#   from that file, and universe.py is the only thing that writes it — so
#   with no schedule, the list simply aged. By 2026-07-30 it was 20.8
#   hours old and the scan had shrunk to 47 symbols against a cap of 150,
#   meaning LOCKBOT was hunting across under a third of the market it was
#   configured for and nobody had told it to stop.
#
#   Order matters: universe.py rewrites the file from scratch, so
#   universe_volatility.py must run AFTER it, never before.
#
#   Nothing ran watchdog.py either. It exists specifically to notice the
#   controller dying — which health_monitor.py cannot do, because it runs
#   inside the controller.
#
# WHAT WAS STALE
#   "LockBot Weekday Start" pointed at C:\Users\jtmed\OneDrive\Documents\
#   MedlockBot — an old copy of the project. It was disabled, so harmless,
#   but an autostart task aimed at the wrong folder is the kind of thing
#   that gets re-enabled a year later and quietly runs the wrong code.
#
# Run from an ordinary PowerShell. These are per-user tasks and need no
# elevation.

$ErrorActionPreference = "Stop"

$Project = "C:\LockBot\Medlockbot"
$Python  = "$Project\.venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Output "Interpreter not found at $Python"
    exit 1
}

function Register-LockBotTask {
    param(
        [string]$Name,
        [string]$Arguments,
        $Trigger,
        [string]$Description
    )

    $action = New-ScheduledTaskAction -Execute $Python `
                                      -Argument $Arguments `
                                      -WorkingDirectory $Project

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Output "  replaced : $Name"
    } else {
        Write-Output "  created  : $Name"
    }

    Register-ScheduledTask -TaskName $Name `
                           -Action $action `
                           -Trigger $Trigger `
                           -Settings $settings `
                           -Description $Description | Out-Null
}

Write-Output "LOCKBOT schedule"
Write-Output "================"

# --- Universe rebuild -------------------------------------------------
# 07:45 local, weekdays. Before the 08:30 premarket window and well
# before the open, so the scanner starts the day on a fresh list.
$universeTrigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "07:45"

Register-LockBotTask -Name "LockBot Universe Rebuild" `
    -Arguments "$Project\build_universe.py" `
    -Trigger $universeTrigger `
    -Description "Rebuild universe.csv, then apply the movement filter. Must run in that order."

# --- Watchdog ---------------------------------------------------------
# Every 15 minutes. It is the only thing that can notice the controller
# process itself dying, so it must not live inside the controller.
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

Register-LockBotTask -Name "LockBot Watchdog" `
    -Arguments "$Project\watchdog.py" `
    -Trigger $watchdogTrigger `
    -Description "External check that the controller is alive and the heartbeat is fresh."

# --- Nightly learning pass --------------------------------------------
# After the close, once the day's shadow trades can resolve.
$learnTrigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "18:30"

Register-LockBotTask -Name "LockBot Learning Pass" `
    -Arguments "$Project\lockbot_learn.py" `
    -Trigger $learnTrigger `
    -Description "Resolve what is newly true and write it to brain_memory.md."

# --- Remove the stale task --------------------------------------------
$stale = Get-ScheduledTask -TaskName "LockBot Weekday Start" -ErrorAction SilentlyContinue

if ($stale) {
    $target = $stale.Actions[0].Arguments
    if ($target -like "*OneDrive*") {
        Unregister-ScheduledTask -TaskName "LockBot Weekday Start" -Confirm:$false
        Write-Output "  removed  : LockBot Weekday Start (pointed at the old OneDrive copy)"
    } else {
        Write-Output "  kept     : LockBot Weekday Start (does not point at OneDrive - check it by hand)"
    }
}

Write-Output ""
Write-Output "Current LOCKBOT tasks:"
Get-ScheduledTask | Where-Object { $_.TaskName -like "LockBot*" } |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
        "  {0,-28} {1,-10} next: {2}" -f $_.TaskName, $_.State, $info.NextRunTime
    }
