@echo off

REM Remote access to LOCKBOT from your phone.
REM
REM Requires TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS in .env.
REM Without them the bot exits immediately rather than polling, so this
REM window closing straight away means the token is missing, not that it
REM crashed. Run "lockbot_telegram.py --check" to see which.
REM
REM The bot answers questions and places no trades. The single exception
REM is /flatten, which cancels orders and closes positions -- it exists
REM for when the code looks wrong, not when a trade looks bad.

cd /d "C:\LockBot\Medlockbot"

title LOCKBOT TELEGRAM

"C:\LockBot\Medlockbot\.venv\Scripts\python.exe" -u "C:\LockBot\Medlockbot\lockbot_telegram.py" --run

echo.
echo LOCKBOT TELEGRAM exited with code %ERRORLEVEL%.
pause
