@echo off
REM External watchdog -- runs OUTSIDE the controller so it can notice the
REM controller itself dying. One-shot: checks, alerts if needed, exits.
REM Scheduled every 15 minutes as LOCKBOT_Watchdog.
cd /d C:\LockBot\Medlockbot
.venv\Scripts\python.exe watchdog.py >> watchdog.log 2>&1
