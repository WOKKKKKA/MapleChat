' ============================================
'  MapleChat wu hei kuang qi dong qi (EXE ban)
'  shuang ji ben wen jian ji ke qi dong, bu hui chu xian ren he ming ling hang chuang kou
' ============================================
Option Explicit
Dim fso, sh, base, exePath, q
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
q = Chr(34)
base = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = base & "\MapleChat.exe"
If Not fso.FileExists(exePath) Then
    MsgBox "找不到 MapleChat.exe。" & vbCrLf & "请把本文件放在与 MapleChat.exe 同一个文件夹里。", 16, "MapleChat"
    WScript.Quit 1
End If
sh.CurrentDirectory = base
sh.Run q & exePath & q, 0, False
