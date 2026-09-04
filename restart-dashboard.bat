@echo off
REM One-shot restart for the CAP Dashboard Scheduled Task — stop, a brief
REM pause for the process tree (wscript -> cmd -> uv -> python) to fully
REM release port 8421 including uvicorn's own graceful-shutdown sequence,
REM then start again. Needed after any backend (.py) change — those aren't
REM hot-reloaded, only Jinja2 templates are. See src/web/CLAUDE.md's CAP
REM section for the 2026-09-04 incident this replaces: starting without
REM stopping first (or not giving the old process time to fully exit) left
REM a stale process holding the port while Task Scheduler reported success.
powershell -NoProfile -Command "Stop-ScheduledTask -TaskName 'CAP Dashboard'; Start-Sleep -Seconds 3; Start-ScheduledTask -TaskName 'CAP Dashboard'"
echo Restarted. Give it a few seconds, then check http://cap.lan:8421/
