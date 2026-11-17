' ============================================
'  MapleChat wu hei kuang qi dong qi (yuan ma ban)
'  shuang ji ben wen jian ji ke qi dong, bu hui chu xian ren he ming ling hang chuang kou
' ============================================
Option Explicit
Dim fso, sh, base, rt, py, target, q
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
q = Chr(34)
base   = fso.GetParentFolderName(WScript.ScriptFullName)
rt     = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\MapleChat_rt")
py     = rt & "\Scripts\pythonw.exe"
target = base & "\main.py"
If Not fso.FileExists(py) Then
    MsgBox "运行环境不存在：" & rt & vbCrLf & "请先双击运行【安装环境.bat】自动创建。", 16, "MapleChat"
    WScript.Quit 1
End If
If Not fso.FileExists(target) Then
    MsgBox "找不到 main.py。" & vbCrLf & "请把本文件放在 main.py 同一个文件夹里。", 16, "MapleChat"
    WScript.Quit 1
End If
sh.CurrentDirectory = base
sh.Run q & py & q & " " & q & target & q, 0, False
