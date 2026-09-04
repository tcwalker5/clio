@echo off
REM Go to project root directory
cd /d C:\Users\TEDMINI\projects\clio

REM Scan open/pending matters for those with both Date of Marriage and Date
REM of Separation set, and (re)write the computed "Length of Marriage" field
REM only where it's out of date. Intended to run daily via Windows Task
REM Scheduler — silent no-op on days with nothing to update. See
REM src/web/CLAUDE.md's CAP section for why this is its own small task
REM rather than a shared scheduler, same pattern as sync-ringcentral.bat.
call uv run src\date_calculator.py
