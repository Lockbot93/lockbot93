@echo off

REM Conversation interface: wake word, open dialogue, and typed input.
REM
REM This is launched from the Startup folder rather than Task Scheduler on
REM purpose. A scheduled task set to run "whether the user is logged on or
REM not" lands in session 0, which has no audio device and no microphone --
REM the brain would start, hear silence forever, and never speak. The
REM Startup folder always runs in the interactive desktop session, where
REM the microphone and speakers actually exist.

cd /d "C:\LockBot\Medlockbot"

title LOCKBOT BRAIN

"C:\LockBot\Medlockbot\.venv\Scripts\python.exe" -u "C:\LockBot\Medlockbot\lockbot_brain.py" --chat --wake

REM Keep the window open if it exits, so a crash is visible rather than
REM silently closing and leaving no way to talk to LOCKBOT.
echo.
echo LOCKBOT BRAIN exited with code %ERRORLEVEL%.
pause
