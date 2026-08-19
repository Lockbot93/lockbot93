@echo off
REM Live-session snapshot of what LOCKBOT is actually allowed to buy.
REM Read only -- no orders, no state. Writes a CSV, this log, and a push.
cd /d C:\LockBot\Medlockbot
.venv\Scripts\python.exe options_tradeable.py --save --notify --top 100 > options_tradeable_latest.txt 2>&1
