@echo off
REM Go to project root directory
cd /d C:\Users\TEDMINI\projects\clio

REM Sync dependencies (handles fresh clone or a moved project)
echo Syncing dependencies...
call uv sync

REM Start the dashboard, listening on the LAN (not just localhost)
echo Starting Clio Dashboard on http://0.0.0.0:8421 ...
call uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8421

REM Keep window open if it crashes immediately
pause
