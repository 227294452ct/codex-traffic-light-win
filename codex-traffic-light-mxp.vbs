' Codex Traffic Light MXP - self-healing autostart guard (Windows)
' Runs at login, ensures GUI + Hermes watcher + Qwen Work watcher are alive,
' every 30s respawns any that were killed (e.g. by "cleanup/boost" tools
' from other apps). Check-before-launch so duplicate guards never double-spawn.
Set ws = CreateObject("WScript.Shell")
pyw     = "__PYW__"
dataDir = ws.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\CodexTrafficLight"
appDir  = dataDir & "\app"
guiCmd  = """" & pyw & """ """ & appDir & "\run_app.pyw"""
watCmd  = """" & pyw & """ """ & appDir & "\hermes-watcher.py"""
qwenCmd = """" & pyw & """ """ & appDir & "\qwen-watcher.py"""

Function IsRunning(cmdline)
  IsRunning = False
  On Error Resume Next
  Dim svc, col, p
  Set svc = GetObject("winmgmts:\\.\root\cimv2")
  ' NOTE: .InstancesOf("Win32_Process") fails on this machine
  ' ("interface not supported") - ExecQuery is the reliable path.
  Set col = svc.ExecQuery("SELECT Name, CommandLine FROM Win32_Process")
  For Each p In col
    ' only real interpreter processes count; transient shells (bash, powershell)
    ' whose command line merely *mentions* the script name must be ignored
    If Not IsNull(p.Name) And Not IsNull(p.CommandLine) Then
      If LCase(CStr(p.Name)) = "pythonw.exe" Or LCase(CStr(p.Name)) = "python.exe" Then
        If InStr(CStr(p.CommandLine), cmdline) > 0 Then
          IsRunning = True
          Exit For
        End If
      End If
    End If
  Next
  On Error GoTo 0
End Function

Sub EnsureRunning(cmd, marker)
  If Not IsRunning(marker) Then
    LogLine "LAUNCH " & marker
    On Error Resume Next
    ws.Run cmd, 0, False
    If Err.Number <> 0 Then LogLine "WSRUN-ERR " & Err.Number & " " & Err.Description
    On Error GoTo 0
  Else
    LogLine "SKIP " & marker
  End If
End Sub

Sub LogLine(msg)
  On Error Resume Next
  Dim fso, f, ts
  Set fso = CreateObject("Scripting.FileSystemObject")
  Set ts = fso.OpenTextFile(dataDir & "\guard.log", 8, True)
  ts.WriteLine Now & " " & msg
  ts.Close
  On Error GoTo 0
End Sub

' --- user-turned-off flag -------------------------------------------------
' The light's "退出并不再自动启动" menu item writes light-off.json; while it
' exists the guard must NOT respawn the GUI (light-on.vbs clears the flag).
offFlag = dataDir & "\light-off.json"

Function UserOff()
  UserOff = False
  On Error Resume Next
  Dim fso2
  Set fso2 = CreateObject("Scripting.FileSystemObject")
  UserOff = fso2.FileExists(offFlag)
  On Error GoTo 0
End Function

Dim guiOffSeen : guiOffSeen = False
Sub EnsureGui(cmd, marker)
  If UserOff() Then
    If Not guiOffSeen Then
      LogLine "GUI OFF (user quit; run light-on.vbs to re-enable)"
      guiOffSeen = True
    End If
  Else
    guiOffSeen = False
    EnsureRunning cmd, marker
  End If
End Sub

' initial pass (also check-first, so a second guard never double-spawns)
EnsureGui guiCmd, "app\run_app.pyw"
EnsureRunning watCmd, "app\hermes-watcher.py"
EnsureRunning qwenCmd, "app\qwen-watcher.py"

Do
  WScript.Sleep 30000
  EnsureGui guiCmd, "app\run_app.pyw"
  EnsureRunning watCmd, "app\hermes-watcher.py"
  EnsureRunning qwenCmd, "app\qwen-watcher.py"
Loop
