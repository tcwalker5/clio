' Hidden-window launcher for the CAP dashboard's Windows Scheduled Task.
' Deliberately different from start-dashboard-silent.vbs's fire-and-forget
' launch (WshShell.Run(..., False)) — that's correct for a desktop shortcut
' a person double-clicks and expects control back immediately, but wrong
' here: with False, wscript.exe would exit right after launching the batch,
' so Task Scheduler would mark the task "finished" while uvicorn was still
' running detached underneath it — its own crash-restart and "don't start a
' new instance" settings would then be watching the wrong process. The
' third argument True makes wscript.exe block until start-dashboard-service.bat
' itself exits (i.e. until uvicorn exits), so Task Scheduler's task state
' actually tracks whether the dashboard is alive.
' WshShell.Run's return value IS the launched process's exit code — but only
' if something actually does something with it. Without an explicit
' WScript.Quit, this script just ends after the Run call and wscript.exe
' exits 0 regardless of what the batch/uvicorn actually did — confirmed live
' 2026-09-04: Task Scheduler recorded LastTaskResult 0 ("success") for a run
' whose own log showed uvicorn failing to bind port 8421 (WinError 10048,
' exit code 3) because a stale prior instance was still holding it. Passing
' the real exit code through is what makes "restart on failure" actually see
' failures.
Set WshShell = CreateObject("WScript.Shell")
exitCode = WshShell.Run("""C:\Users\TEDMINI\projects\clio\start-dashboard-service.bat""", 0, True)
WScript.Quit(exitCode)
