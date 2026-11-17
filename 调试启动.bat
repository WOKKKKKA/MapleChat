@echo off
chcp 65001 >nul
title MapleChat 调试启动（带控制台）
set "RT=%LOCALAPPDATA%\MapleChat_rt"
if not exist "%RT%\Scripts\python.exe" (
  echo [错误] 运行环境不存在，请先双击运行 安装环境.bat
  pause
  exit /b 1
)
"%RT%\Scripts\python.exe" "%~dp0main.py"
echo.
echo ---- 程序已退出，上方为错误信息（如有） ----
pause
