@echo off
chcp 65001 >nul
title MapleChat 环境安装
set "RT=%LOCALAPPDATA%\MapleChat_rt"
set "PY="

if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [错误] 未找到可用的 Python 3.9+，请先安装 Python。
  pause
  exit /b 1
)

echo 使用解释器: %PY%
echo 安装位置:   %RT%   ^(刻意放在 C 盘根目录，规避 Windows 260 字符长路径限制^)
echo.

if not exist "%RT%\Scripts\python.exe" (
  "%PY%" -m venv "%RT%"
)

"%RT%\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"%RT%\Scripts\python.exe" -m pip install mss opencv-python pillow numpy requests rapidocr-onnxruntime -i https://pypi.tuna.tsinghua.edu.cn/simple

echo.
echo 验证安装...
"%RT%\Scripts\python.exe" -c "from rapidocr_onnxruntime import RapidOCR; import mss, cv2, requests; print('OK: 依赖齐全')"
echo.
echo 安装完成，双击 启动.bat 即可运行。
pause
