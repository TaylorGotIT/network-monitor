@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON=python"
if exist "venv\Scripts\python.exe" set "PYTHON=venv\Scripts\python.exe"

echo 正在启动网络质量监控，按 Ctrl+C 停止...
"%PYTHON%" app.py %*

echo.
echo 服务已退出（错误码 %ERRORLEVEL%）。
pause
