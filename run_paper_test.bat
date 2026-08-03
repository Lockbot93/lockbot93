@echo off
cd /d C:\Users\jtmed\OneDrive\Documents\MedlockBot
call .venv\Scripts\activate.bat
python test_paper_order.py >> paper_order_log.txt 2>&1