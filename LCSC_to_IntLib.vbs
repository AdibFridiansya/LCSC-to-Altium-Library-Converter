Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "E:\LCSC-to-IntLib"
WshShell.Run """E:\LCSC-to-IntLib\.venv\Scripts\pythonw.exe"" ""E:\LCSC-to-IntLib\app.py""", 0, False
