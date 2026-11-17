@echo off
chcp 65001 >nul
title MapleChat 启动器
set "RT=%LOCALAPPDATA%\MapleChat_rt"
if not exist "%RT%\Scripts\pythonw.exe" (
  echo [错误] 运行环境不存在，请先双击运行 安装环境.bat
  pause
  exit /b 1
)
start "" "%RT%\Scripts\pythonw.exe" "%~dp0main.py"
