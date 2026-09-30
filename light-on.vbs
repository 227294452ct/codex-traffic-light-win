' Codex Traffic Light MXP - re-enable after "退出并不再自动启动".
' Deletes the light-off flag and starts the GUI once; the autostart guard
' takes over again from the next 30s tick.
Set fso = CreateObject("Scripting.FileSystemObject")
Set ws2 = CreateObject("WScript.Shell")
dataDir = ws2.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\CodexTrafficLight"
flag = dataDir & "\light-off.json"
If fso.FileExists(flag) Then fso.DeleteFile flag
pyw = "__PYW__"
appDir = dataDir & "\app"
ws2.Run """" & pyw & """ """ & appDir & "\run_app.pyw""", 0, False
