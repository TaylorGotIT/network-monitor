@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set PORT=8888

set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  echo 停止监听 %PORT% 的进程 PID %%a
  taskkill /PID %%a /F >nul 2>&1
  set FOUND=1
)

if "!FOUND!"=="0" echo 没有找到监听 %PORT% 的进程。
pause
