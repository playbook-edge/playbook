@echo off
REM resolve_trades.bat
REM Automatically resolves paper trades against real MLB box scores.
REM Scheduled via Task Scheduler to run at 11:30 PM ET nightly.

cd /d C:\Users\patri\CodingProjects\playbook
python alerts/paper_trading.py auto_resolve
