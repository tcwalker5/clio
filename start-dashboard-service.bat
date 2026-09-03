@echo off
REM Unattended entry point for the CAP dashboard's Windows Scheduled Task —
REM see CLAUDE.md's "CAP — Collier Automation Platform" section. Unlike
REM start-dashboard.bat, this has no `pause`: Task Scheduler has no console
REM to send a keypress to, so a `pause` here would hang the task forever
REM after any crash instead of letting it exit with a real code Task
REM Scheduler can react to — that exit code is what "restart on failure"
REM actually watches.
cd /d C:\Users\TEDMINI\projects\clio
if not exist logs mkdir logs

REM %date% is locale-dependent (MM/DD/YYYY vs DD/MM/YYYY etc.) — go through
REM PowerShell for a reliable yyyyMMdd stamp regardless of this machine's
REM regional settings, same reasoning every Python script here uses
REM datetime.strftime instead of shelling out to `date`.
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyyMMdd\")"') do set DATESTAMP=%%i
set LOGFILE=logs\dashboard_service_%DATESTAMP%.log

echo [%date% %time%] Syncing dependencies... >> "%LOGFILE%"
call uv sync >> "%LOGFILE%" 2>&1

echo [%date% %time%] Starting Collier Automation Platform on http://0.0.0.0:8421 ... >> "%LOGFILE%"
call uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8421 >> "%LOGFILE%" 2>&1

echo [%date% %time%] uvicorn exited with code %errorlevel% >> "%LOGFILE%"
exit /b %errorlevel%
