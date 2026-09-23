@echo off
REM One-shot restart for the CAP Dashboard Scheduled Task. Needed after any
REM backend (.py) change -- those aren't hot-reloaded, only Jinja2 templates
REM are. See src/web/CLAUDE.md's CAP section for the 2026-09-04 incident this
REM script was originally written to fix (start without stop first, or no
REM grace period -> stale process holds the port while Task Scheduler
REM reports success).
REM
REM 2026-09-08: that original fix (stop, sleep 3, start) was NOT enough on
REM its own -- confirmed live when this exact script plus a manual
REM Stop-ScheduledTask both left a uvicorn process from FOUR DAYS EARLIER
REM (2026-09-04) still bound to port 8421 and still serving stale code.
REM Stop-ScheduledTask only reliably kills the task's own tracked root
REM process (wscript.exe) -- it does not reliably reap uvicorn's own
REM process tree underneath it if that tree has drifted outside Task
REM Scheduler's job object, which is exactly what happened. The Scheduled
REM Task's own LastTaskResult (3) correctly recorded that the subsequent
REM Start-ScheduledTask attempt failed to bind the port -- but nothing was
REM checking that result, so the failure was silent and the stale process
REM just kept serving.
REM
REM Fix: after stopping the task, explicitly find and force-kill any
REM process whose command line shows it's a leftover uvicorn on port 8421
REM (catches the whole wrapper chain -- uvicorn.exe launcher -> python.exe
REM -> python.exe -- not just whichever PID happens to own the socket),
REM then verify the port is actually free before starting a new instance.
REM Refuses to start rather than launching a second instance that would
REM just fail to bind again, the same silent failure as above.

echo Stopping CAP Dashboard task...
powershell -NoProfile -Command "Stop-ScheduledTask -TaskName 'CAP Dashboard' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 3; $stale = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'port 8421' }; foreach ($p in $stale) { Write-Host \"Stop-ScheduledTask didn't kill PID $($p.ProcessId) (started $($p.CreationDate)) -- force-killing it.\"; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; if ($stale) { Start-Sleep -Seconds 2 }; if (Get-NetTCPConnection -LocalPort 8421 -State Listen -ErrorAction SilentlyContinue) { Write-Host 'ERROR: port 8421 is still occupied after force-kill -- refusing to start a new instance, it would only fail to bind. Investigate manually: Get-NetTCPConnection -LocalPort 8421'; exit 1 }"
if errorlevel 1 (
    echo Restart aborted -- port 8421 could not be freed. See error above.
    exit /b 1
)

echo Starting CAP Dashboard task...
powershell -NoProfile -Command "Start-ScheduledTask -TaskName 'CAP Dashboard'"
timeout /t 3 /nobreak >nul

REM Record when this restart happened, not just print it -- the console
REM window closes, but logs\last-restart.txt sticks around so "when was
REM this last restarted" has an answer without digging through the dated
REM dashboard_service_*.log files. Overwritten each run (last restart only,
REM not a history) -- through PowerShell for a locale-independent stamp,
REM same reasoning start-dashboard-service.bat already uses for its own
REM DATESTAMP rather than trusting %date%/%time% directly.
if not exist logs mkdir logs
powershell -NoProfile -Command "$info = Get-ScheduledTaskInfo -TaskName 'CAP Dashboard'; $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'; Write-Host \"Task last result: $($info.LastTaskResult) (0 = launched OK so far; nonzero means it failed, e.g. still couldn't bind the port)\"; \"$stamp -- Start-ScheduledTask issued (LastTaskResult: $($info.LastTaskResult))\" | Set-Content 'logs\last-restart.txt'; Write-Host \"Restarted at $stamp\""

echo Give it a few seconds, then check http://cap.lan:8421/
