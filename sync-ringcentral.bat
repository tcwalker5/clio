@echo off
REM Go to project root directory
cd /d C:\Users\TEDMINI\projects\clio

REM Build the RingCentral directory CSV from Clio and, only if it changed since
REM the last run, open a browser to the RingCentral import page. Intended to be
REM run daily via Windows Task Scheduler — silent no-op on days with no change.
call uv run src\ringcentral_directory.py
