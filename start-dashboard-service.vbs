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
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\TEDMINI\projects\clio\start-dashboard-service.bat""", 0, True
