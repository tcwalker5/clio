@echo off
REM Go to project root directory
cd /d C:\Users\TEDMINI\projects\clio

REM Build the RingCentral directory CSV from Clio and, only if it changed since
REM the last run, open a browser to the RingCentral import page. Was run daily via
REM the "Clio RingCentral Sync" Scheduled Task — that task is disabled as of
REM 2026-09-29 (sync is manual now), see reference/ringcentral.md.
call uv run src\ringcentral_directory.py
