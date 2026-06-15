' Launch the comparator tray app with no console window.
Set sh = CreateObject("WScript.Shell")
appDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.CurrentDirectory = appDir
sh.Run "pythonw.exe """ & appDir & "tray_app.py""", 0, False
